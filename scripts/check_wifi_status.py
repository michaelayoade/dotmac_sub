#!/usr/bin/env python3
"""
Check WiFi data collection status from GenieACS.
Run periodically to monitor when WiFi data becomes available.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

GENIEACS_URL = "http://localhost:7557"

def main():
    print(f"=== WiFi Data Collection Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    
    # Get all devices
    try:
        with urllib.request.urlopen(f'{GENIEACS_URL}/devices', timeout=30) as resp:
            devices = json.load(resp)
    except Exception as e:
        print(f"Error connecting to GenieACS: {e}")
        sys.exit(1)
    
    # Analyze devices
    total = len(devices)
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    
    informed_24h = 0
    with_landevice = 0
    with_wifi = 0
    
    wifi_params = []
    
    for device in devices:
        # Check last inform
        last_inform = device.get('_lastInform')
        if last_inform:
            try:
                ts = datetime.fromisoformat(last_inform.replace('Z', '+00:00'))
                if ts > last_24h:
                    informed_24h += 1
            except:
                pass
        
        # Check for LANDevice (WiFi container)
        lan = device.get('InternetGatewayDevice', {}).get('LANDevice')
        if lan:
            with_landevice += 1
            
            # Check for WLANConfiguration
            wlan = lan.get('1', {}).get('WLANConfiguration')
            if wlan:
                with_wifi += 1
                # Extract SSID if available
                for idx in ['1', '2', '5']:
                    if idx in wlan:
                        ssid = wlan[idx].get('SSID', {}).get('_value')
                        if ssid:
                            wifi_params.append({
                                'device': device['_id'][:35],
                                'ssid': ssid
                            })
    
    print(f"Total devices: {total}")
    print(f"Informed in last 24h: {informed_24h}")
    print(f"With LANDevice data: {with_landevice}")
    print(f"With WiFi data: {with_wifi}")
    
    if wifi_params:
        print(f"\nDevices with WiFi SSID:")
        for wp in wifi_params[:10]:
            print(f"  {wp['device']}: {wp['ssid']}")
        if len(wifi_params) > 10:
            print(f"  ... and {len(wifi_params) - 10} more")
    else:
        print("\nNo WiFi data collected yet.")
        print("WiFi data will be collected when devices inform and execute queued tasks.")
    
    # Check pending tasks
    try:
        with urllib.request.urlopen(f'{GENIEACS_URL}/tasks', timeout=10) as resp:
            tasks = json.load(resp)
        getpv_tasks = [t for t in tasks if t.get('name') == 'getParameterValues']
        print(f"\nPending getParameterValues tasks: {len(getpv_tasks)}")
    except:
        print("\nCould not check pending tasks")

if __name__ == '__main__':
    main()
