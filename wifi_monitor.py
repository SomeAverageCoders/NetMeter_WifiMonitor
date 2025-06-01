#!/usr/bin/env python3
"""
WiFi Usage Monitor - Tracks network usage per device and uploads to database
Run this script 24/7 on each device to monitor WiFi usage
"""

import psutil
import requests
import json
import time
import uuid
import subprocess
import platform
import sqlite3
import os
from datetime import datetime, timedelta
from threading import Thread
import hashlib


class WiFiUsageMonitor:
    def __init__(self, config_file="wifi_monitor_config.json"):
        self.config_file = config_file
        self.load_config()
        self.device_id = self.get_device_id()
        self.local_db_path = "local_usage.db"
        self.setup_local_db()
        self.last_upload = datetime.now()

    def load_config(self):
        """Load configuration from JSON file"""
        default_config = {
            "api_endpoint": "http://your-server.com/api/usage",
            "upload_interval": 300,  # 5 minutes
            "api_key": "your_api_key_here",
            "target_wifi_ssid": "YOUR_WIFI_NAME"
        }

        try:
            with open(self.config_file, 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            print(f"Config file not found. Creating {self.config_file} with default values.")
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=4)
            self.config = default_config
            print("Please update the config file with your server details!")

    def get_device_id(self):
        """Generate unique device identifier"""
        try:
            # Get MAC address of the primary network interface
            mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                            for elements in range(0, 2 * 6, 2)][::-1])
            device_name = platform.node()
            return hashlib.md5(f"{mac}_{device_name}".encode()).hexdigest()[:12]
        except:
            return str(uuid.uuid4())[:12]

    def setup_local_db(self):
        """Setup local SQLite database for storing usage data"""
        conn = sqlite3.connect(self.local_db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                device_id TEXT,
                wifi_ssid TEXT,
                bytes_sent INTEGER,
                bytes_received INTEGER,
                total_bytes INTEGER,
                uploaded BOOLEAN DEFAULT FALSE
            )
        ''')

        conn.commit()
        conn.close()

    def get_current_wifi_ssid(self):
        """Get currently connected WiFi SSID"""
        try:
            system = platform.system()

            if system == "Windows":
                result = subprocess.run(['netsh', 'wlan', 'show', 'profiles'],
                                        capture_output=True, text=True)
                # Parse Windows netsh output to get current SSID
                result = subprocess.run(['netsh', 'wlan', 'show', 'interfaces'],
                                        capture_output=True, text=True)
                for line in result.stdout.split('\n'):
                    if 'SSID' in line and ':' in line:
                        return line.split(':')[1].strip()

            elif system == "Darwin":  # macOS
                result = subprocess.run(
                    ['/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport', '-I'],
                    capture_output=True, text=True)
                for line in result.stdout.split('\n'):
                    if 'SSID:' in line:
                        return line.split(':')[1].strip()

            elif system == "Linux":
                result = subprocess.run(['iwgetid', '-r'], capture_output=True, text=True)
                return result.stdout.strip()

        except Exception as e:
            print(f"Error getting WiFi SSID: {e}")

        return None

    def get_network_usage(self):
        """Get current network usage statistics"""
        try:
            stats = psutil.net_io_counters()
            return {
                'bytes_sent': stats.bytes_sent,
                'bytes_received': stats.bytes_recv,
                'total_bytes': stats.bytes_sent + stats.bytes_recv
            }
        except Exception as e:
            print(f"Error getting network stats: {e}")
            return None

    def store_usage_locally(self, wifi_ssid, usage_data):
        """Store usage data in local SQLite database"""
        try:
            conn = sqlite3.connect(self.local_db_path)
            cursor = conn.cursor()

            cursor.execute('''
                INSERT INTO usage_data 
                (timestamp, device_id, wifi_ssid, bytes_sent, bytes_received, total_bytes)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                datetime.now().isoformat(),
                self.device_id,
                wifi_ssid,
                usage_data['bytes_sent'],
                usage_data['bytes_received'],
                usage_data['total_bytes']
            ))

            conn.commit()
            conn.close()
            return True

        except Exception as e:
            print(f"Error storing data locally: {e}")
            return False

    def upload_to_server(self):
        """Upload accumulated usage data to server"""
        try:
            conn = sqlite3.connect(self.local_db_path)
            cursor = conn.cursor()

            # Get unuploaded data
            cursor.execute('''
                SELECT id, timestamp, device_id, wifi_ssid, bytes_sent, bytes_received, total_bytes
                FROM usage_data 
                WHERE uploaded = FALSE
                ORDER BY timestamp
            ''')

            unuploaded_data = cursor.fetchall()

            if not unuploaded_data:
                conn.close()
                return True

            # Prepare data for upload
            upload_payload = {
                'device_id': self.device_id,
                'data': []
            }

            for row in unuploaded_data:
                upload_payload['data'].append({
                    'local_id': row[0],
                    'timestamp': row[1],
                    'device_id': row[2],
                    'wifi_ssid': row[3],
                    'bytes_sent': row[4],
                    'bytes_received': row[5],
                    'total_bytes': row[6]
                })

            # Upload to server
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {self.config['api_key']}"
            }

            response = requests.post(
                self.config['api_endpoint'],
                json=upload_payload,
                headers=headers,
                timeout=30
            )

            if response.status_code == 200:
                # Mark as uploaded
                ids_to_mark = [str(row[0]) for row in unuploaded_data]
                cursor.execute(f'''
                    UPDATE usage_data 
                    SET uploaded = TRUE 
                    WHERE id IN ({','.join(['?' for _ in ids_to_mark])})
                ''', ids_to_mark)

                conn.commit()
                print(f"Successfully uploaded {len(unuploaded_data)} records")

            else:
                print(f"Upload failed: {response.status_code} - {response.text}")

            conn.close()
            return response.status_code == 200

        except Exception as e:
            print(f"Error uploading to server: {e}")
            return False

    def cleanup_old_data(self, days_to_keep=30):
        """Clean up old local data that has been uploaded"""
        try:
            conn = sqlite3.connect(self.local_db_path)
            cursor = conn.cursor()

            cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).isoformat()

            cursor.execute('''
                DELETE FROM usage_data 
                WHERE uploaded = TRUE AND timestamp < ?
            ''', (cutoff_date,))

            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()

            if deleted_count > 0:
                print(f"Cleaned up {deleted_count} old records")

        except Exception as e:
            print(f"Error cleaning up old data: {e}")

    def monitor_usage(self):
        """Main monitoring loop"""
        print(f"Starting WiFi usage monitor for device: {self.device_id}")
        print(f"Target WiFi: {self.config['target_wifi_ssid']}")

        last_stats = None
        last_check_time = datetime.now()

        while True:
            try:
                current_time = datetime.now()
                current_wifi = self.get_current_wifi_ssid()

                # Only track usage if connected to target WiFi
                if current_wifi == self.config['target_wifi_ssid']:
                    current_stats = self.get_network_usage()

                    if current_stats and last_stats:
                        # Calculate usage delta
                        time_delta = (current_time - last_check_time).total_seconds()

                        if time_delta > 0:  # Avoid division by zero
                            usage_delta = {
                                'bytes_sent': max(0, current_stats['bytes_sent'] - last_stats['bytes_sent']),
                                'bytes_received': max(0,
                                                      current_stats['bytes_received'] - last_stats['bytes_received']),
                                'total_bytes': 0
                            }
                            usage_delta['total_bytes'] = usage_delta['bytes_sent'] + usage_delta['bytes_received']

                            # Store if there's actual usage
                            if usage_delta['total_bytes'] > 0:
                                self.store_usage_locally(current_wifi, usage_delta)
                                print(
                                    f"Logged {usage_delta['total_bytes']} bytes at {current_time.strftime('%H:%M:%S')}")

                    last_stats = current_stats

                else:
                    print(f"Not connected to target WiFi. Current: {current_wifi}")
                    last_stats = None

                last_check_time = current_time

                # Upload data periodically
                if (current_time - self.last_upload).total_seconds() > self.config['upload_interval']:
                    print("Uploading data to server...")
                    if self.upload_to_server():
                        self.last_upload = current_time
                        self.cleanup_old_data()

                # Wait before next check
                time.sleep(60)  # Check every minute

            except KeyboardInterrupt:
                print("\nStopping monitor...")
                break
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                time.sleep(60)  # Wait before retrying

    def get_local_stats(self, days=7):
        """Get local usage statistics"""
        try:
            conn = sqlite3.connect(self.local_db_path)
            cursor = conn.cursor()

            start_date = (datetime.now() - timedelta(days=days)).isoformat()

            cursor.execute('''
                SELECT 
                    DATE(timestamp) as date,
                    SUM(total_bytes) as daily_usage
                FROM usage_data 
                WHERE timestamp > ? AND wifi_ssid = ?
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
            ''', (start_date, self.config['target_wifi_ssid']))

            results = cursor.fetchall()
            conn.close()

            print(f"\nLocal usage stats (last {days} days):")
            print("-" * 40)
            total_usage = 0
            for date, usage in results:
                usage_mb = usage / (1024 * 1024)
                total_usage += usage
                print(f"{date}: {usage_mb:.2f} MB")

            total_mb = total_usage / (1024 * 1024)
            print(f"\nTotal: {total_mb:.2f} MB")

            return results

        except Exception as e:
            print(f"Error getting local stats: {e}")
            return []


def main():
    """Main function to run the WiFi monitor"""
    monitor = WiFiUsageMonitor()

    # Show current configuration
    print("WiFi Usage Monitor Configuration:")
    print(f"Device ID: {monitor.device_id}")
    print(f"Target WiFi: {monitor.config['target_wifi_ssid']}")
    print(f"Upload interval: {monitor.config['upload_interval']} seconds")
    print(f"API endpoint: {monitor.config['api_endpoint']}")
    print("-" * 50)

    # Start monitoring
    try:
        # Start monitoring in a separate thread
        monitor_thread = Thread(target=monitor.monitor_usage)
        monitor_thread.daemon = True
        monitor_thread.start()

        # Keep main thread alive and handle commands
        while True:
            try:
                cmd = input(
                    "\nCommands: 'stats' (show usage), 'upload' (force upload), 'quit' (exit): ").strip().lower()

                if cmd == 'stats':
                    monitor.get_local_stats()
                elif cmd == 'upload':
                    print("Uploading data...")
                    if monitor.upload_to_server():
                        print("Upload successful!")
                    else:
                        print("Upload failed!")
                elif cmd == 'quit' or cmd == 'exit':
                    break
                else:
                    print("Unknown command")

            except KeyboardInterrupt:
                break

    except Exception as e:
        print(f"Error: {e}")

    print("WiFi monitor stopped.")


if __name__ == "__main__":
    main()