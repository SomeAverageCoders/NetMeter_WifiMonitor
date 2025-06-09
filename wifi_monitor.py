#!/usr/bin/env python3
"""
Network Usage Monitor - Installation and Runtime Script
Monitors network usage for specific SSIDs and stores data in PostgreSQL database
"""

import os
import sys
import time
import json
import logging
import subprocess
import threading
import argparse
from datetime import datetime
from pathlib import Path
from decimal import Decimal
import base64
import uuid
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

# Configuration paths
if getattr(sys, 'frozen', False):
    CONFIG_DIR = Path(os.path.dirname(sys.executable)) / '.wifi_monitor'
else:
    CONFIG_DIR = Path.home() / '.wifi_monitor'

CONFIG_FILE = CONFIG_DIR / "config.json"
DB_CONFIG_FILE = CONFIG_DIR / "db_config.json"
LOG_FILE = CONFIG_DIR / "monitor.log"
DATA_FILE = CONFIG_DIR / "network_usage.json"
SCRIPT_FILE = CONFIG_DIR / "wifi_monitor.py"

# Ensure config directory exists
CONFIG_DIR.mkdir(exist_ok=True)

# Configure logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class DatabaseManager:
    def __init__(self, db_config_path=DB_CONFIG_FILE):
        self.db_config_path = db_config_path
        self.db_config = self.load_db_config()
        self.conn = None
        
    def load_db_config(self):
        """Load database configuration from file"""
        if self.db_config_path.exists():
            try:
                with open(self.db_config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Error loading database config: {e}")
        return None
        
    def connect(self):
        """Connect to PostgreSQL database"""
        if not self.db_config:
            logging.error("No database configuration found")
            return False
            
        try:
            self.conn = psycopg2.connect(
                dbname=self.db_config['dbname'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                host=self.db_config['host'],
                port=self.db_config['port']
            )
            return True
        except Exception as e:
            logging.error(f"Database connection error: {e}")
            return False
            
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            
    def get_device_info(self, mac_address):
        """Get device and owner information"""
        if not self.conn:
            return None
            
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = sql.SQL("""
                    SELECT 
                        d.id as device_id, 
                        d.name as device_name, 
                        u.id as user_id,
                        u.name as user_name,
                        array_remove(array_agg(gm.groupId), NULL) as group_ids
                    FROM device d
                    JOIN "user" u ON d.owner_id = u.id
                    LEFT JOIN device_groups dg ON d.id = dg.device_id
                    LEFT JOIN group_members_user gm ON dg.group_id = gm.groupId
                    WHERE d.mac_address = %s
                    GROUP BY d.id, u.id
                """)
                cur.execute(query, (mac_address,))
                result = cur.fetchone()
                if result and result['group_ids'] is None:
                    result['group_ids'] = []
                return result
        except Exception as e:
            logging.error(f"Error getting device info: {e}")
            return None
            
    def get_wifi_config(self, ssid, group_id):
        """Get WiFi configuration for SSID and group"""
        if not self.conn:
            return None
            
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = sql.SQL("""
                    SELECT wc.* 
                    FROM wifi_configuration wc
                    JOIN "group" g ON wc.id = g.wifiConfigId
                    WHERE wc.ssid = %s AND g.id = %s
                """)
                cur.execute(query, (ssid, group_id))
                return cur.fetchone()
        except Exception as e:
            logging.error(f"Error getting WiFi config: {e}")
            return None
            
    def save_usage_data(self, device_id, group_id, ssid, date, download_bytes, upload_bytes):
        """Save network usage data"""
        if not self.conn:
            return False
            
        try:
            with self.conn.cursor() as cur:
                download_decimal = Decimal(download_bytes).quantize(Decimal('0.00'))
                upload_decimal = Decimal(upload_bytes).quantize(Decimal('0.00'))
                
                check_query = sql.SQL("""
                    SELECT id FROM device_usage 
                    WHERE device_id = %s AND group_id = %s 
                    AND usage_date = %s AND ssid = %s
                """)
                cur.execute(check_query, (device_id, group_id, date, ssid))
                existing = cur.fetchone()
                
                if existing:
                    update_query = sql.SQL("""
                        UPDATE device_usage 
                        SET download_bytes = download_bytes + %s,
                            upload_bytes = upload_bytes + %s
                        WHERE id = %s
                    """)
                    cur.execute(update_query, (download_decimal, upload_decimal, existing[0]))
                else:
                    insert_query = sql.SQL("""
                        INSERT INTO device_usage 
                        (device_id, group_id, usage_date, ssid, download_bytes, upload_bytes)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """)
                    cur.execute(insert_query, 
                            (device_id, group_id, date, ssid, download_decimal, upload_decimal))
                
                self.conn.commit()
                return True
        except Exception as e:
            logging.error(f"Error saving usage data: {e}")
            if self.conn:
                self.conn.rollback()
            return False
            
    def check_quota_exceeded(self, group_id, ssid, date):
        """Check if group's daily quota has been exceeded for SSID"""
        if not self.conn:
            return False
            
        try:
            with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = sql.SQL("""
                    SELECT 
                        wc.dailyUsageLimitPerMember,
                        COALESCE(SUM(du.download_bytes + du.upload_bytes), 0) as total_usage
                    FROM wifi_configuration wc
                    JOIN "group" g ON wc.id = g.wifiConfigId
                    LEFT JOIN device_usage du ON du.group_id = g.id 
                                            AND du.usage_date = %s
                                            AND du.ssid = %s
                    WHERE g.id = %s AND wc.ssid = %s
                    GROUP BY wc.dailyUsageLimitPerMember
                """)
                cur.execute(query, (date, ssid, group_id, ssid))
                result = cur.fetchone()
                
                if result and result['dailyUsageLimitPerMember'] > 0:
                    return result['total_usage'] >= result['dailyUsageLimitPerMember']
                return False
        except Exception as e:
            logging.error(f"Error checking quota: {e}")
            return False

    def test_connection(self):
        """Test database connection"""
        try:
            if not self.db_config:
                return False, "No database configuration found"
                
            conn = psycopg2.connect(
                dbname=self.db_config['dbname'],
                user=self.db_config['user'],
                password=self.db_config['password'],
                host=self.db_config['host'],
                port=self.db_config['port']
            )
            conn.close()
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)

class NetworkMonitor:
    def __init__(self, config):
        self.target_ssid = config.get('target_ssid', '')
        self.poll_interval = config.get('poll_interval', 5)
        self.debug = config.get('debug', False)
        self.last_bytes_sent = 0
        self.last_bytes_recv = 0
        self.usage_data = self.load_data()
        self.connected_to_target = False
        self.mac_address = self.get_mac_address()
        
        self.db = DatabaseManager()
        self.db_connected = self.db.connect()
        
        self.device_info = None
        self.current_group_id = None
        self.wifi_config = None
        if self.db_connected and self.mac_address:
            self.device_info = self.db.get_device_info(self.mac_address)

    def get_mac_address(self):
        """Get MAC address of primary network interface"""
        try:
            if sys.platform == "win32":
                output = subprocess.check_output("ipconfig /all", shell=True).decode()
                for line in output.split('\n'):
                    if "Physical Address" in line:
                        return line.split(":")[1].strip().replace("-", ":")
            else:
                import fcntl
                import socket
                import struct
                
                interfaces = ["eth0", "wlan0", "en0"]
                for interface in interfaces:
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        info = fcntl.ioctl(s.fileno(), 0x8927, 
                                          struct.pack('256s', bytes(interface[:15], 'utf-8')))
                        return ':'.join('%02x' % b for b in info[18:24])
                    except:
                        continue
        except Exception as e:
            logging.error(f"Error getting MAC address: {e}")
        return None
    
    def load_data(self):
        """Load existing usage data"""
        if DATA_FILE.exists():
            try:
                with open(DATA_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_data(self):
        """Save usage data to file"""
        with open(DATA_FILE, 'w') as f:
            json.dump(self.usage_data, f, indent=2)
    
    def get_current_ssid(self):
        """Get current WiFi SSID - cross platform"""
        try:
            system = sys.platform
            
            if system == "darwin":
                cmd = ["/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport", "-I"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n'):
                    if ' SSID' in line:
                        return line.split(':')[1].strip()
                        
            elif system == "win32":
                cmd = ["netsh", "wlan", "show", "interfaces"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n'):
                    if 'SSID' in line and 'BSSID' not in line:
                        return line.split(':')[1].strip()
                        
            else:
                cmd = ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n'):
                    if line.startswith('yes:'):
                        return line.split(':')[1].strip()
                        
        except Exception as e:
            if self.debug:
                logging.error(f"Error getting SSID: {e}")
        return None
    
    def get_network_stats(self):
        """Get network statistics - cross platform"""
        try:
            if sys.platform == "win32":
                try:
                    ps_cmd = [
                        "powershell", "-Command",
                        "Get-NetAdapterStatistics | Select-Object -Property ReceivedBytes, SentBytes | ConvertTo-Json"
                    ]
                    output = subprocess.check_output(ps_cmd, stderr=subprocess.DEVNULL).decode()
                    data = json.loads(output)
                    
                    total_recv = 0
                    total_sent = 0
                    
                    if isinstance(data, list):
                        for adapter in data:
                            total_recv += adapter.get('ReceivedBytes', 0)
                            total_sent += adapter.get('SentBytes', 0)
                    elif isinstance(data, dict):
                        total_recv = data.get('ReceivedBytes', 0)
                        total_sent = data.get('SentBytes', 0)
                    
                    if total_recv > 0 or total_sent > 0:
                        return total_recv, total_sent
                except:
                    pass
                
                try:
                    cmd = ["netstat", "-e"]
                    output = subprocess.check_output(cmd).decode()
                    lines = output.strip().split('\n')
                    
                    for i, line in enumerate(lines):
                        if 'Bytes' in line and 'Received' in line and 'Sent' in line:
                            if i + 1 < len(lines):
                                data_line = lines[i + 1].strip()
                                parts = data_line.split()
                                if len(parts) >= 3 and parts[0] == 'Bytes':
                                    return int(parts[1]), int(parts[2])
                except:
                    pass
                    
            else:
                with open('/proc/net/dev', 'r') as f:
                    lines = f.readlines()
                
                for line in lines[2:]:
                    if ':' in line:
                        parts = line.split()
                        interface = parts[0].strip(':')
                        if interface != 'lo':
                            bytes_recv = int(parts[1])
                            bytes_sent = int(parts[9])
                            return bytes_recv, bytes_sent
                            
        except Exception as e:
            if self.debug:
                logging.error(f"Error getting network stats: {e}")
        return 0, 0
    
    def update_usage(self):
        """Update network usage if connected to target SSID"""
        current_ssid = self.get_current_ssid()
        
        if current_ssid != self.target_ssid:
            if self.connected_to_target:
                logging.info(f"Disconnected from target network '{self.target_ssid}'")
                self.connected_to_target = False
                self.last_bytes_recv = 0
                self.last_bytes_sent = 0
                self.current_group_id = None
                self.wifi_config = None
            return
        
        if not self.connected_to_target:
            logging.info(f"Connected to target network '{current_ssid}'")
            self.connected_to_target = True
            bytes_recv, bytes_sent = self.get_network_stats()
            self.last_bytes_recv = bytes_recv
            self.last_bytes_sent = bytes_sent
            
            if self.db_connected and self.device_info and self.device_info.get('group_ids'):
                for group_id in self.device_info['group_ids']:
                    if not group_id:  # Skip None values
                        continue
                    wifi_config = self.db.get_wifi_config(current_ssid, group_id)
                    if wifi_config:
                        self.current_group_id = group_id
                        self.wifi_config = wifi_config
                        logging.info(f"Matched WiFi config for group {group_id} (SSID: {current_ssid})")
                        break
            
            return
        
        bytes_recv, bytes_sent = self.get_network_stats()
        
        if self.last_bytes_recv > 0 and bytes_recv >= self.last_bytes_recv:
            delta_recv = bytes_recv - self.last_bytes_recv
            delta_sent = bytes_sent - self.last_bytes_sent
        else:
            delta_recv = 0
            delta_sent = 0
        
        self.last_bytes_recv = bytes_recv
        self.last_bytes_sent = bytes_sent
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        if today not in self.usage_data:
            self.usage_data[today] = {
                "download": 0,
                "upload": 0,
                "sessions": []
            }
        
        if delta_recv > 0 or delta_sent > 0:
            self.usage_data[today]["download"] += delta_recv
            self.usage_data[today]["upload"] += delta_sent
            
            self.usage_data[today]["sessions"].append({
                "time": datetime.now().isoformat(),
                "ssid": current_ssid,
                "download": delta_recv,
                "upload": delta_sent
            })
            
            self.save_data()
            
            if (self.db_connected and self.device_info and self.current_group_id and 
                delta_recv + delta_sent > 0):
                
                quota_exceeded = self.db.check_quota_exceeded(
                    self.current_group_id, 
                    current_ssid,
                    today
                )
                if quota_exceeded:
                    logging.warning(f"Daily quota exceeded for group {self.current_group_id} on SSID {current_ssid}")
                else:
                    success = self.db.save_usage_data(
                        self.device_info['device_id'],
                        self.current_group_id,
                        current_ssid,
                        today,
                        delta_recv,
                        delta_sent
                    )
                    if not success:
                        logging.error("Failed to save usage data to database")
            
            total_today = (self.usage_data[today]["download"] + 
                          self.usage_data[today]["upload"]) / (1024 * 1024)
            
            if self.debug:
                print(f"Usage on '{current_ssid}': DL:{delta_recv/1024:.1f}KB UL:{delta_sent/1024:.1f}KB Total:{total_today:.2f}MB")
    
    def run(self):
        """Main monitoring loop"""
        logging.info(f"Network monitor started for SSID: {self.target_ssid}")
        if self.device_info:
            logging.info(f"Monitoring for device: {self.device_info['device_name']} (MAC: {self.mac_address})")
            logging.info(f"Device owner: {self.device_info['user_name']} (ID: {self.device_info['user_id']})")
        
        print(f"Monitoring network usage for SSID: {self.target_ssid}")
        if self.device_info:
            print(f"Device: {self.device_info['device_name']} (Owner: {self.device_info['user_name']})")
        print(f"Data saved to database and local file: {DATA_FILE}")
        print("Press Ctrl+C to stop...")
        
        try:
            while True:
                self.update_usage()
                time.sleep(self.poll_interval)
        finally:
            self.db.close()

class Installer:
    def __init__(self):
        self.config = self.load_config()
    
    def load_config(self):
        """Load existing configuration"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}
    
    def save_config(self, config):
        """Save configuration"""
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    
    def get_available_ssids(self):
        """Get list of available WiFi networks"""
        ssids = []
        try:
            if sys.platform == "win32":
                cmd = ["netsh", "wlan", "show", "networks"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n'):
                    if 'SSID' in line and ':' in line:
                        ssid = line.split(':', 1)[1].strip()
                        if ssid and ssid not in ssids:
                            ssids.append(ssid)
            elif sys.platform == "darwin":
                cmd = ["/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport", "-s"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n')[1:]:
                    parts = line.split()
                    if parts:
                        ssid = parts[0]
                        if ssid not in ssids:
                            ssids.append(ssid)
            else:
                cmd = ["nmcli", "-f", "SSID", "dev", "wifi"]
                output = subprocess.check_output(cmd).decode()
                for line in output.split('\n')[1:]:
                    ssid = line.strip()
                    if ssid and ssid not in ssids:
                        ssids.append(ssid)
        except:
            pass
        return ssids
    
    def get_current_ssid(self):
        """Get currently connected SSID"""
        monitor = NetworkMonitor({})
        return monitor.get_current_ssid()
    
    def configure_database(self):
        """Configure database connection settings"""
        print("\nDatabase Configuration")
        print("=" * 30)
        
        db_config = {}
        if DB_CONFIG_FILE.exists():
            try:
                with open(DB_CONFIG_FILE, 'r') as f:
                    db_config = json.load(f)
            except:
                pass
        
        db_config['dbname'] = input(f"Database name [{db_config.get('dbname', '')}]: ").strip() or db_config.get('dbname', '')
        db_config['user'] = input(f"Username [{db_config.get('user', '')}]: ").strip() or db_config.get('user', '')
        db_config['password'] = input(f"Password [{'*' * len(db_config.get('password', ''))}]: ").strip() or db_config.get('password', '')
        db_config['host'] = input(f"Host [{db_config.get('host', 'localhost')}]: ").strip() or db_config.get('host', 'localhost')
        db_config['port'] = input(f"Port [{db_config.get('port', '5432')}]: ").strip() or db_config.get('port', '5432')
        
        print("\nTesting database connection...")
        db_manager = DatabaseManager()
        db_manager.db_config = db_config
        success, message = db_manager.test_connection()
        
        if success:
            with open(DB_CONFIG_FILE, 'w') as f:
                json.dump(db_config, f, indent=2)
            print("Database connection successful! Configuration saved.")
            return True
        else:
            print(f"Connection failed: {message}")
            return False

    def interactive_setup(self):
        """Interactive setup wizard"""
        current_step = 1
        target_ssid = None
        poll_interval = 5
        setup_cancelled = False

        while not setup_cancelled:
            print("\n" + "="*50)
            print("Network Usage Monitor - Setup Wizard")
            print("="*50 + "\n")

            if current_step == 1:
                print("1. Configure database connection")
                print("2. Skip database configuration (local monitoring only)")
                print("0. Cancel setup")
                
                choice = input("\nEnter your choice (0-2): ").strip()
                
                if choice == "0":
                    setup_cancelled = True
                    print("\nSetup cancelled.")
                elif choice == "1":
                    if self.configure_database():
                        current_step = 2
                elif choice == "2":
                    print("\nDatabase configuration skipped. Data will be stored locally only.")
                    current_step = 2
                else:
                    print("Invalid selection, please try again.")

            elif current_step == 2:
                current_ssid = self.get_current_ssid()
                if current_ssid:
                    print(f"Currently connected to: {current_ssid}")

                print("\nScanning for available networks...")
                available_ssids = self.get_available_ssids()

                print("\nSelect the network to monitor:")
                print("1. Use current network" + (f" ({current_ssid})" if current_ssid else " (not connected)"))
                
                if available_ssids:
                    print("2. Choose from available networks")
                print("3. Enter network name manually")
                print("0. Go back to database configuration")

                choice = input("\nEnter your choice (0-3): ").strip()

                if choice == "0":
                    current_step = 1
                elif choice == "1" and current_ssid:
                    target_ssid = current_ssid
                    current_step = 3
                elif choice == "2" and available_ssids:
                    while True:
                        print("\nAvailable networks:")
                        for i, ssid in enumerate(available_ssids, 1):
                            print(f"{i}. {ssid}")
                        print("0. Go back")

                        try:
                            idx = input("\nSelect network number (or 0 to go back): ").strip()
                            if idx == "0":
                                break
                            idx = int(idx) - 1
                            if 0 <= idx < len(available_ssids):
                                target_ssid = available_ssids[idx]
                                current_step = 3
                                break
                            else:
                                print("Invalid selection, please try again.")
                        except ValueError:
                            print("Please enter a valid number.")
                elif choice == "3":
                    while True:
                        target_ssid = input("\nEnter network name (SSID) or '0' to go back: ").strip()
                        if target_ssid == "0":
                            break
                        if target_ssid:
                            current_step = 3
                            break
                        else:
                            print("Network name cannot be empty")
                else:
                    print("Invalid selection, please try again.")

            elif current_step == 3:
                print(f"\nNetwork to monitor: {target_ssid}")
                print("\nConfigure polling interval:")
                print("1. Use default (5 seconds)")
                print("2. Enter custom interval")
                print("0. Go back to network selection")

                choice = input("\nEnter your choice (0-2): ").strip()

                if choice == "0":
                    current_step = 2
                elif choice == "1":
                    poll_interval = 5
                    current_step = 4
                elif choice == "2":
                    while True:
                        interval = input("\nEnter polling interval in seconds (1-60): ").strip()
                        try:
                            poll_interval = int(interval)
                            if 1 <= poll_interval <= 60:
                                current_step = 4
                                break
                            else:
                                print("Interval must be between 1 and 60 seconds")
                        except ValueError:
                            print("Please enter a valid number.")
                else:
                    print("Invalid selection, please try again.")

            elif current_step == 4:
                print("\nConfiguration Summary:")
                if DB_CONFIG_FILE.exists():
                    with open(DB_CONFIG_FILE, 'r') as f:
                        db_config = json.load(f)
                    print(f"- Database: {db_config['host']}:{db_config['port']}/{db_config['dbname']}")
                else:
                    print("- Database: Not configured (local storage only)")
                print(f"- Network: {target_ssid}")
                print(f"- Poll interval: {poll_interval} seconds")
                print("\n1. Save configuration and exit")
                print("2. Change database configuration")
                print("3. Change network")
                print("4. Change polling interval")
                print("0. Cancel without saving")

                choice = input("\nEnter your choice (0-4): ").strip()

                if choice == "1":
                    self.config = {
                        'target_ssid': target_ssid,
                        'poll_interval': poll_interval,
                        'debug': False
                    }
                    self.save_config(self.config)
                    print("\nConfiguration saved successfully!")
                    return True
                elif choice == "2":
                    current_step = 1
                elif choice == "3":
                    current_step = 2
                elif choice == "4":
                    current_step = 3
                elif choice == "0":
                    setup_cancelled = True
                    print("\nSetup cancelled.")
                else:
                    print("Invalid selection, please try again.")

        return False
    
    def create_startup_script(self):
        """Create platform-specific startup configuration"""
        current_script = os.path.abspath(__file__)
        
        with open(SCRIPT_FILE, 'w') as f:
            with open(current_script, 'r') as source:
                f.write(source.read())
        
        system = sys.platform
        
        if system == "darwin":
            self._create_macos_startup()
        elif system == "win32":
            self._create_windows_startup()
        else:
            self._create_linux_startup()
    
    def _create_macos_startup(self):
        """Create macOS LaunchAgent"""
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.network.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{SCRIPT_FILE}</string>
        <string>--run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_FILE}</string>
</dict>
</plist>"""
        
        plist_path = Path.home() / "Library/LaunchAgents/com.network.monitor.plist"
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(plist_path, 'w') as f:
            f.write(plist_content)
        
        print(f"\nCreated LaunchAgent at: {plist_path}")
        print("\nTo enable auto-start, run:")
        print(f"launchctl load {plist_path}")
    
    def _create_windows_startup(self):
        """Create Windows startup entry"""
        import winreg
        
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 
                               r"Software\Microsoft\Windows\CurrentVersion\Run", 
                               0, winreg.KEY_SET_VALUE)
            
            command = f'"{sys.executable}" "{SCRIPT_FILE}" --run'
            winreg.SetValueEx(key, "NetworkMonitor", 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
            
            print("\nAdded to Windows startup registry")
            
            batch_file = CONFIG_DIR / "start_monitor.bat"
            with open(batch_file, 'w') as f:
                f.write(f'@echo off\n"{sys.executable}" "{SCRIPT_FILE}" --run\n')
            
            print(f"\nCreated batch file for manual start: {batch_file}")
            
        except Exception as e:
            print(f"\nError adding to startup: {e}")
    
    def _create_linux_startup(self):
        """Create Linux systemd service"""
        service_content = f"""[Unit]
Description=Network Usage Monitor
After=network.target

[Service]
Type=simple
ExecStart={sys.executable} {SCRIPT_FILE} --run
Restart=always
User={os.getenv('USER')}

[Install]
WantedBy=default.target"""
        
        service_dir = Path.home() / ".config/systemd/user"
        service_dir.mkdir(parents=True, exist_ok=True)
        service_path = service_dir / "network-monitor.service"
        
        with open(service_path, 'w') as f:
            f.write(service_content)
        
        print(f"\nCreated systemd service at: {service_path}")
        print("\nTo enable auto-start, run:")
        print("systemctl --user enable network-monitor.service")
    
    def install(self):
        """Run installation process"""
        if self.interactive_setup():
            print("\nDo you want to set up automatic startup? (y/n): ", end='')
            if input().strip().lower() == 'y':
                self.create_startup_script()
            
            print("\nInstallation complete!")
            print("\nYou can now:")
            print("1. Run the monitor manually with: python network_monitor.py --run")
            print("2. View usage data at: " + str(DATA_FILE))
            print("3. Check logs at: " + str(LOG_FILE))
            
            print("\nStart monitoring now? (y/n): ", end='')
            if input().strip().lower() == 'y':
                self.run_monitor()
    
    def run_monitor(self):
        """Run the monitor with current configuration"""
        if not self.config.get('target_ssid'):
            print("No configuration found. Please run setup first.")
            print("Run: python network_monitor.py --install")
            return
        
        monitor = NetworkMonitor(self.config)
        try:
            monitor.run()
        except KeyboardInterrupt:
            print("\nMonitor stopped.")

def main():
    debug_log = CONFIG_DIR / 'startup_debug.log'
    with open(debug_log, 'w') as f:
        f.write(f"Starting at {datetime.now()}\n")
        f.write(f"sys.argv: {sys.argv}\n")
        f.write(f"sys.path: {sys.path}\n")
        f.write(f"os.getcwd(): {os.getcwd()}\n")
        f.write(f"CONFIG_DIR: {CONFIG_DIR}\n")
        
    parser = argparse.ArgumentParser(description='Network Usage Monitor')
    parser.add_argument('--install', action='store_true', help='Run installation wizard')
    parser.add_argument('--run', action='store_true', help='Run the monitor')
    parser.add_argument('--config', action='store_true', help='Show current configuration')
    parser.add_argument('--stats', action='store_true', help='Show usage statistics')
    
    args = parser.parse_args()
    
    installer = Installer()
    
    if args.install:
        installer.install()
    elif args.run:
        installer.run_monitor()
    elif args.config:
        if installer.config:
            print("\nCurrent configuration:")
            print(json.dumps(installer.config, indent=2))
            if DB_CONFIG_FILE.exists():
                with open(DB_CONFIG_FILE, 'r') as f:
                    print("\nDatabase configuration:")
                    print(json.dumps(json.load(f), indent=2))
        else:
            print("No configuration found. Run with --install to set up.")
    elif args.stats:
        if DATA_FILE.exists():
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            print("\nNetwork usage statistics:")
            for date, stats in sorted(data.items()):
                total_mb = (stats['download'] + stats['upload']) / (1024 * 1024)
                print(f"\n{date}:")
                print(f"  Download: {stats['download'] / (1024 * 1024):.2f} MB")
                print(f"  Upload: {stats['upload'] / (1024 * 1024):.2f} MB")
                print(f"  Total: {total_mb:.2f} MB")
                print("  Sessions:")
                for session in stats['sessions']:
                    print(f"    {session['time']} - {session['ssid']}: DL:{session['download']/1024:.1f}KB UL:{session['upload']/1024:.1f}KB")
        else:
            print("No usage data found.")
    else:
        installer.install()

if __name__ == "__main__":
    main()