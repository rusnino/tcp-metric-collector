#
# TCP Metrics Collector
#

import argparse
import json
import os
import re
import signal
import sys
from time import sleep

#
# Global Variables
#
DEFAULT_IP = "0.0.0.0"
DEFAULT_SLEEP = 0.1
RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+\:\S+)\s+(\d+\.\d+\.\d+\.\d+\:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"(\S+)\:(\S+)"

def is_valid_ip(ip):
    """
    Is valid IP address
    """
    match_ip = re.findall(r"^(\d+\.\d+\.\d+\.\d+)$",ip)
    return True if match_ip else False
    
def print_tcp_metrics(tcp_metrics):
    """
    Parse and print collected data
    """
    print_data = {"curr_session": ""}
    
    def _parse_tcp_metrics(metrics):
        """
        Parse line with TCP metrics
        """
        parsed_metrics = {
            "cwnd": 0,
            "rtt": 0,
            "mss": 0,
            "ssthresh": 0,
            "send": 0,
            "unacked": 0,
            "retrans": 0,
        }
        
        for param in metrics.split(" "):
            lookup_param = re.findall(RE_TCP_METRIC_PARAM_LOOKUP,param)
            if not lookup_param or lookup_param[0][0].lower() not in parsed_metrics.keys():
                continue
                
            param_name = lookup_param[0][0]
            param_value = lookup_param[0][1]
            
            # Save param values
            parsed_metrics[param_name] = param_value
            
        return json.dumps(parsed_metrics)
    
    # Parse collected SS data for TCP Sessions
    for tcp_session in tcp_metrics:
        for line in tcp_session.splitlines():
            if "tcp " in line:
                if "CLOSING" in line:
                    continue
                
                # Lookup TCP session
                lookup_tcp_session = re.findall(RE_TCP_SESSION_LOOKUP,line.strip())
                
                if not lookup_tcp_session:
                    continue
                    
                tcp_session_key = "{src}_{dst}".format(src=lookup_tcp_session[0][0],dst=lookup_tcp_session[0][1])
                print_data["curr_session"] = tcp_session_key
                if tcp_session_key not in print_data.keys():
                    print_data[tcp_session_key] = {"curr_timestamp" : 0.0, "metrics": []}
                    
            if "wscale" in line and print_data["curr_session"]:
                # Parse current TCP Metrics of session
                line = line.replace("send ","send:") if "send " in line else line
                tcp_session_metrics = _parse_tcp_metrics(line)
                if not tcp_session_metrics:
                    continue
                    
                curr_session = print_data["curr_session"]
                curr_timestamp = print_data[curr_session]["curr_timestamp"] + DEFAULT_SLEEP
                
                # Save TCP Metrics for current timestamp
                print_data[curr_session]["metrics"].append((curr_timestamp, tcp_session_metrics))
                
                # Update current timestamp
                print_data[curr_session]["curr_timestamp"] = curr_timestamp
                
    # Print parsed TCP Metrics data pre each TCP Sessions
    for tcp, tcp_metrics in print_data.items():
        if "curr_session" in tcp:
            continue
            
        if not tcp_metrics["metrics"]:
            continue
        
        print(chr(10))
        print("======== START TCP SESSION ({s}) ========".format(s=tcp.replace("_"," <--> ")))
        
        for metric_data in tcp_metrics["metrics"]:
            
            # Print each record
            print("{t} - {m}".format(t=metric_data[0],m=metric_data[1][1:][:-1]))
                
        
        print("======== END TCP SESSION ({s}) ========".format(s=tcp.replace("_"," <--> ")))
        print(chr(10))

def run():
    """
    Run TCP Metrics Collector
    """
    
    # Init main dict store for TCP Metrcis
    tcp_metrics = []
    
    # Collect args
    parser = argparse.ArgumentParser(description="TCP Metrics Collector")
    parser.add_argument("-a", action="store", dest="ip", default=DEFAULT_IP, type=str)
    args = parser.parse_args()
    
    if args.ip == DEFAULT_IP:
        print("The IP address is mandatory. Please setup by arg -a")
        sys.exit()
        
    if not is_valid_ip(args.ip):
        print("The IP address {ip_address} is not valid.".format(ip_address=args.ip))
        sys.exit()
    
    def SignalHandler(*args):
        """
        Handler of Ctrl + C
        """
        print_tcp_metrics(tcp_metrics)
        sys.exit()

    signal.signal(signal.SIGINT, SignalHandler)
    
    # Start collecting data
    while True:
        stream = os.popen("ss -i dst {ip_address} | grep {ip_address} -A 1".format(ip_address=args.ip))
        output = stream.read()
        tcp_metrics.append(output)
        sleep(DEFAULT_SLEEP)

if __name__ == '__main__':
    print("INFO: The TCP metrics are collecting each {sleep_v} second.".format(sleep_v=DEFAULT_SLEEP))
    print("Press Ctrl + C to stop collecting....")
    run()