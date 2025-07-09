import pandas as pd  # Manejo de datos en DataFrames
import time  # Manejo de timestamps
import joblib  # Para cargar el modelo de IA entrenado
import numpy as np  # Cálculos matemáticos avanzados
from collections import deque  # Para gestionar historial de métricas
from sklearn.preprocessing import MinMaxScaler  # Normalización de datos
from sklearn.impute import SimpleImputer  # Manejo de valores nulos
import mysql.connector  # Conexión con MySQL
import time  # Para manejar timestamps
import struct  # Para empaquetar/desempaquetar datos binarios (timestamps)
import threading
# Ryu Framework
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, arp
from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link

class CongestionMonitoring(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(CongestionMonitoring, self).__init__(*args, **kwargs)
        self.datapaths = {}
        self.port_bandwidth = {}
        self.link_metrics= {}
        self.prev_port_stats={}
        self.mac_to_port={}
        self.mac_to_location={}
        self.flow_table={}
        self.packet_in_count={}
        self.metrics_history=[]
        self.throughput_history={}
        self.delay_history={}
        self.port_delay_jitter={}    
        self.lock = threading.Lock()  # 🔒 Crear un lock para sincronización    
        self.db_connection=mysql.connector.connect(host="localhost",user="ryu_user",password="password",database="sdn_metrics")
        self.db_cursor=self.db_connection.cursor()
        try:
            self.model = joblib.load('/home/chrisvb/modelo_rf_congestion_06.pkl')
            #self.scaler=joblib.load('/home/chrisvb/modelos/scaler.pkl')
            self.logger.info("Modelo Cargado Correctamente.")
        except Exception as e:
            self.logger.error(f"Error en la carga: {e}")

        
        #Iniciar monitoreo de métricas
        self.monitor_thread_traffic= hub.spawn(self._monitor)
        self.monitor_thread_timestamp = hub.spawn(self._send_timestamp_packet)
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Registra switches y solicita descripción de puertos"""
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        #self.logger.info(f"🔌 Switch {datapath.id} conectado. Solicitando info de puertos.")
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPPortDescStatsRequest(datapath, 0))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def _port_desc_stats_reply_handler(self, ev):
        """Guarda velocidades de los puertos"""
        dpid = ev.msg.datapath.id
        #Tomada debido que en la mayoria de pruebas es el promedio del trafico real entregado
        #578+578+593+628+616+616+621
        empiric_max_speed=610e6
        for port in ev.msg.body:
            self.port_bandwidth[port.port_no] = port.curr_speed if port.curr_speed>empiric_max_speed else empiric_max_speed
    def _monitor(self):
        """Monitorea tráfico en los switches periódicamente"""
        while True:
            for dp in self.datapaths.values():
                self._request_traffic_stats(dp)
            hub.sleep(1)

    def _send_timestamp_packet(self):
        """Envía un paquete OpenFlow con un timestamp embebido."""
        last_sent_time={}
        while True:
            if self.datapaths:
                for dpid, datapath in self.datapaths.items():
                    current_time=time.time()
                    if dpid not in last_sent_time or (current_time-last_sent_time[dpid]):
                        #self.logger.info(f"Enviando paquete con timestamp desde switch {dpid}")
                        timestamp = time.time()
                        self._inject_packet(datapath, timestamp)
            hub.sleep(1)  # Intervalo de envío
     
    def _request_traffic_stats(self, datapath):
        """Solicita estadísticas de puertos"""
        parser = datapath.ofproto_parser
        datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, ofproto_v1_3.OFPP_ANY))

    def _inject_packet(self, datapath, timestamp):
        """Genera y envía un paquete OpenFlow con el timestamp."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Convertir timestamp a bytes
        timestamp_bytes = struct.pack('!d', timestamp)

        # Usar una MAC destino especial para evitar interferencias con SimpleSwitch13
        mac_dest = "00:00:00:00:00:99"
        # Crear paquete Ethernet
        pkt = packet.Packet()
        eth = ethernet.ethernet(dst=mac_dest, src="00:00:00:00:00:01", ethertype=0x0800)
        pkt.add_protocol(eth)
 
        pkt.add_protocol(timestamp_bytes)
 
        pkt.serialize()
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=pkt.data
        )
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """Recibe un paquete con timestamp y calcula el delay."""
        msg = ev.msg
        datapath = msg.datapath
        dpid = datapath.id
        timestamp_now = time.time()

        pkt=packet.Packet(msg.data)
        eth=pkt.get_protocols(ethernet.ethernet)[0]

        # Filtrar solo los paquetes de timestamp con la MAC destino especial
        if eth.dst != "00:00:00:00:00:99":
            return  # Ignorar otros paquetes

        # Verificar que el paquete es lo suficientemente grande para contener un timestamp
        if len(msg.data) < 22:  # 14 bytes Ethernet + 8 bytes Timestamp
            self.logger.error(f"⚠️ Paquete demasiado pequeño para contener timestamp ({len(msg.data)} bytes)")
            return
        #print(f"Switch {dpid} recibio un paquete con {len(msg.data)} de size")
        #self.logger.info(f"Con informacion {msg.data}")        
        try:
            # Saltar los primeros 14 bytes de la cabecera Ethernet
            timestamp_bytes = msg.data[14:22]  # Extraer los 8 bytes del timestamp

            # Convertir de binario a número flotante
            timestamp_sent = struct.unpack('!d', timestamp_bytes)[0]

            # Calcular delay
            delay = timestamp_now - timestamp_sent
            # Obtener Puerto de entrada de paquete
            in_port=msg.match['in_port']

            # **Cálculo del jitter**
            if dpid not in self.delay_history:
                self.delay_history[dpid] = {}
            if in_port not in self.delay_history[dpid]:
                self.delay_history[dpid][in_port] = deque(maxlen=10)

            # Agregar el nuevo delay al historial
            self.delay_history[dpid][in_port].append(delay)
           

            # Calcular jitter si hay al menos 2 valores en el historial
            jitter = 0
            if len(self.delay_history[dpid][in_port]) > 1:
                last_delay=self.delay_history[dpid][in_port][-1] #Ultimo delay
                prev_delay=self.delay_history[dpid][in_port][-2] #Penultimo delay
                jitter = abs(last_delay-prev_delay)
            if dpid not in self.port_delay_jitter:
                self.port_delay_jitter[dpid]={}
            self.port_delay_jitter[dpid][in_port]=(timestamp_now,delay,jitter)
           
            #self.logger.info(f"⏳ Delay en Switch {dpid},Puerto {in_port}: {delay:.6f} segundos | 🎭 Jitter: {jitter:.6f} segundos")
            

        except Exception as e:
            self.logger.error(f"Error al procesar timestamp de switch {dpid}: {e}")
       
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)    
    def _port_stats_reply_handler(self, ev):
        """Procesa estadísticas de puertos y calcula métricas clave para detectar congestión"""
        timestamp = time.time()
        dpid = ev.msg.datapath.id
        for stat in ev.msg.body:
            port = stat.port_no
            key = (dpid, port)
            in_port=port          
            if port > 100:
                continue
            #link_key = (dpid, in_port, out_port) 
            max_bandwidth = self.port_bandwidth.get(port,1)
            #self.logger.info(f"Velocidad maxima{max_bandwidth}")
            prev_stat = self.prev_port_stats.get(key, None)
            throughput_mbps,avg_pkt_size_rx,avg_pkt_size_tx,label = 0,0,0,0
            throughput_jitter=0
            utilization_pct = 0
            d_tx_delta=0
            d_rx_delta=0
            d_throughput = 0
            d_utilization = 0
            if prev_stat is not None:
                elapsed_time = timestamp - prev_stat["timestamp"]                
                if elapsed_time > 0:
                    # Cálculo de Throughput (Mbps)
                    tx_delta = stat.tx_bytes - prev_stat["tx_bytes"]
                    rx_delta = stat.rx_bytes - prev_stat["rx_bytes"]
                    throughput_mbps = ((tx_delta + rx_delta) * 8) / (elapsed_time * 1e6)
                    
                    # Cálculo de Utilización (%)
                    utilization_pct = ((throughput_mbps * 1e6) / max_bandwidth) * 100 if max_bandwidth > 0 else 0
                    utilization_pct = min(utilization_pct, 100)
                    # **NUEVAS MÉTRICAS: Tasa de Cambio**
                    d_throughput = (throughput_mbps - prev_stat["throughput_mbps"]) / elapsed_time
                    d_utilization = (utilization_pct - prev_stat["utilization_pct"]) / elapsed_time
                    # Deltas de paquetes
                    tx_pkt_delta = stat.tx_packets - prev_stat["tx_packets"]
                    rx_pkt_delta = stat.rx_packets - prev_stat["rx_packets"]
                    # Tamaño promedio de paquete (TX y RX)
                    if tx_pkt_delta > 0:
                        avg_pkt_size_tx = tx_delta / tx_pkt_delta
                    if rx_pkt_delta > 0:
                        avg_pkt_size_rx = rx_delta / rx_pkt_delta
            
            # --- Métrica de Jitter del Throughput ---
            if key not in self.throughput_history:
                self.throughput_history[key] = deque(maxlen=10)
            self.throughput_history[key].append(throughput_mbps)
            # Calcular la desviación estándar como jitter
            if len(self.throughput_history[key]) > 1:
                avg_tp = sum(self.throughput_history[key]) / len(self.throughput_history[key])
                variance = sum((x - avg_tp) ** 2 for x in self.throughput_history[key]) / (len(self.throughput_history[key]) - 1)
                throughput_jitter = variance ** 0.5
            else:
                throughput_jitter = 0
            # **Buscar el delay y jitter más cercano en el tiempo**
            delay, jitter,timestamp_stored= None, None,None
            if dpid in self.port_delay_jitter and port in self.port_delay_jitter[dpid]:
                stored_values = self.port_delay_jitter[dpid][port]
                if isinstance(stored_values,tuple)and len(stored_values)==3:
                    time_stamp_stored,delay,jitter=stored_values
            # Si la diferencia entre `timestamp` actual y `timestamp_stored` es demasiado grande, ignorar valores
                    if timestamp_stored is not None and abs(timestamp - timestamp_stored) > 1:  # 1 segundo de margen
                        delay, jitter = None, None            
            if jitter is None:
                delay,jitter=-1,-1
            if delay>=0:
                metrics={'utilization_pct': utilization_pct,'delay':delay,'jitter':jitter,'throughput_jitter':throughput_jitter}

                # **Preprocesar Datos para IA**
                all_columns = ["utilization_pct", "delay", "jitter", "throughput_jitter"]

                df = pd.DataFrame([metrics])  # Convertir a DataFrame con una fila
                #self.logger.info(f"Data set previo a normalizacion: {df}")
                # Manejo de valores nulos e infinitos
                df.replace([float('inf'), float('-inf')], np.nan, inplace=True)
                imputer = SimpleImputer(strategy='mean')
                df[all_columns] = imputer.fit_transform(df[all_columns])
                # Asegurar que las demás columnas permanezcan sin cambios
                df = df[all_columns]  # Ordenar correctamente las columnas
                #self.logger.info(f"Data set posterior a normalizacion: {df}")

                try:
                    # **Hacer Predicción con el Modelo**
                    label = int(self.model.predict(df))  # 0: No congestión, 1: Moderada, 2: Severa
                    self.ruta_principal=dpid #SE MARCA COMO RUTA PRINCIPAL
 
                except Exception as e:
                    self.logger.info(f"Error en la prediccion del modelo {e}")
                    label=-1
                with self.lock:
                    # Guardar en diccionario por enlace
                    if label == 1:
                        if key not in self.link_metrics:
                            self.link_metrics[key] = 1
                        else:
                            self.link_metrics[key] += 1
                    else:
                        self.link_metrics[key] = 0  # Reiniciar si ya no hay congestión

                self.logger.info(f"📊 [Switch {dpid}] | Enlace: {key} | Predicción: {label} | Historial: {self.link_metrics[key]}")                    
                #self.logger.info(f"Switch : {dpid} ! Throughput: {throughput_mbps:.2f}")
                #self.logger.info(f"Prediccion: {label}")

                self.logger.info(f"|Throughput: {throughput_mbps:.2f}Mbps|Utilization: {utilization_pct:.2f}%|Avg_packet_tx:{avg_pkt_size_tx}|Avg_packet_rx:{avg_pkt_size_rx}|throughput_jitter:{throughput_jitter}")
                self.logger.info(f"|Delay: {delay}|Jitter: {jitter}")

                #self.logger.info(f"Prediccion: {label}")
                   
                    
                sql = """INSERT INTO port_stats
                         (timestamp, dpid, port, throughput_mbps, utilization_pct, avg_pkt_size_rx, avg_pkt_size_tx,throughput_jitter,delay,jitter,d_utilization, d_throughput, tx_bytes, rx_bytes, label)
                         VALUES (%s, %s, %s, %s,%s,%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                values = (timestamp, dpid, port, throughput_mbps, utilization_pct, avg_pkt_size_rx, avg_pkt_size_tx,
                         throughput_jitter,delay,jitter,d_utilization, d_throughput, stat.tx_bytes, stat.rx_bytes,label)
                    
                try:
                    self.db_cursor.execute(sql, values)
                    self.db_connection.commit()
                except mysql.connector.Error as err:
                    self.logger.error(f"Error al insertar en MySQL: {err}")
                    
                        # Actualizar estadísticas previas
            self.prev_port_stats[key] = {
                 "timestamp": timestamp,
                 "tx_bytes": stat.tx_bytes,
                 "rx_bytes": stat.rx_bytes,
                 "tx_packets":stat.tx_packets,
                 "rx_packets":stat.rx_packets,
                 "throughput_mbps": throughput_mbps,
                 "utilization_pct": utilization_pct
            }
