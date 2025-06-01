#!/usr/bin/env python3
"""
WiFi Usage API Server - Receives usage data from monitoring devices
Flask-based REST API with SQLite database
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import json
from datetime import datetime, timedelta
import hashlib
import os

app = Flask(__name__)
CORS(app)

# Configuration
DATABASE_PATH = 'wifi_usage.db'
API_KEY = 'your_secure_api_key_here'  # Change this!


def init_database():
    """Initialize the main database"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    # Usage data table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS usage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            wifi_ssid TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            bytes_sent INTEGER DEFAULT 0,
            bytes_received INTEGER DEFAULT 0,
            total_bytes INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Device registry table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            owner_name TEXT,
            first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT TRUE
        )
    ''')

    # Create indexes for better performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_device_timestamp ON usage_records(device_id, timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_wifi_timestamp ON usage_records(wifi_ssid, timestamp)')

    conn.commit()
    conn.close()


def authenticate_request(req):
    """Simple API key authentication"""
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False

    token = auth_header.replace('Bearer ', '')
    return token == API_KEY


@app.route('/api/usage', methods=['POST'])
def receive_usage_data():
    """Endpoint to receive usage data from devices"""

    # Authentication
    if not authenticate_request(request):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        data = request.json
        device_id = data.get('device_id')
        usage_records = data.get('data', [])

        if not device_id or not usage_records:
            return jsonify({'error': 'Missing device_id or data'}), 400

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        # Update device last seen
        cursor.execute('''
            INSERT OR REPLACE INTO devices (device_id, last_seen)
            VALUES (?, ?)
        ''', (device_id, datetime.now().isoformat()))

        # Insert usage records
        inserted_count = 0
        for record in usage_records:
            try:
                cursor.execute('''
                    INSERT INTO usage_records 
                    (device_id, wifi_ssid, timestamp, bytes_sent, bytes_received, total_bytes)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    device_id,
                    record.get('wifi_ssid', ''),
                    record.get('timestamp', ''),
                    record.get('bytes_sent', 0),
                    record.get('bytes_received', 0),
                    record.get('total_bytes', 0)
                ))
                inserted_count += 1
            except Exception as e:
                print(f"Error inserting record: {e}")
                continue

        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'message': f'Inserted {inserted_count} records for device {device_id}',
            'inserted_count': inserted_count
        })

    except Exception as e:
        print(f"Error processing usage data: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/devices', methods=['GET'])
def get_devices():
    """Get list of all registered devices"""
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT 
                d.device_id,
                d.device_name,
                d.owner_name,
                d.first_seen,
                d.last_seen,
                d.is_active,
                SUM(u.total_bytes) as total_usage
            FROM devices d
            LEFT JOIN usage_records u ON d.device_id = u.device_id
            GROUP BY d.device_id
            ORDER BY d.last_seen DESC
        ''')

        devices = []
        for row in cursor.fetchall():
            devices.append({
                'device_id': row[0],
                'device_name': row[1] or 'Unknown Device',
                'owner_name': row[2] or 'Unknown Owner',
                'first_seen': row[3],
                'last_seen': row[4],
                'is_active': bool(row[5]),
                'total_usage_bytes': row[6] or 0,
                'total_usage_mb': round((row[6] or 0) / (1024 * 1024), 2)
            })

        conn.close()
        return jsonify({'devices': devices})

    except Exception as e:
        print(f"Error getting devices: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/devices/<device_id>', methods=['PUT'])
def update_device(device_id):
    """Update device information"""
    try:
        data = request.json
        device_name = data.get('device_name', '')
        owner_name = data.get('owner_name', '')

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE devices 
            SET device_name = ?, owner_name = ?
            WHERE device_id = ?
        ''', (device_name, owner_name, device_id))

        if cursor.rowcount == 0:
            # Device doesn't exist, create it
            cursor.execute('''
                INSERT INTO devices (device_id, device_name, owner_name)
                VALUES (?, ?, ?)
            ''', (device_id, device_name, owner_name))

        conn.commit()
        conn.close()

        return jsonify({'success': True, 'message': 'Device updated successfully'})

    except Exception as e:
        print(f"Error updating device: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/usage/summary', methods=['GET'])
def get_usage_summary():
    """Get usage summary with billing calculations"""
    try:
        # Get query parameters
        days = int(request.args.get('days', 30))
        wifi_ssid = request.args.get('wifi_ssid', '')

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        # Calculate date range
        start_date = (datetime.now() - timedelta(days=days)).isoformat()

        # Base query
        base_query = '''
            SELECT 
                u.device_id,
                d.device_name,
                d.owner_name,
                SUM(u.total_bytes) as total_usage,
                COUNT(u.id) as record_count,
                MIN(u.timestamp) as first_record,
                MAX(u.timestamp) as last_record
            FROM usage_records u
            LEFT JOIN devices d ON u.device_id = d.device_id
            WHERE u.timestamp >= ?
        '''

        params = [start_date]

        if wifi_ssid:
            base_query += ' AND u.wifi_ssid = ?'
            params.append(wifi_ssid)

        base_query += '''
            GROUP BY u.device_id
            ORDER BY total_usage DESC
        '''

        cursor.execute(base_query, params)

        results = []
        total_usage = 0

        for row in cursor.fetchall():
            usage_bytes = row[3] or 0
            total_usage += usage_bytes

            results.append({
                'device_id': row[0],
                'device_name': row[1] or 'Unknown Device',
                'owner_name': row[2] or 'Unknown Owner',
                'usage_bytes': usage_bytes,
                'usage_mb': round(usage_bytes / (1024 * 1024), 2),
                'usage_gb': round(usage_bytes / (1024 * 1024 * 1024), 2),
                'record_count': row[4],
                'first_record': row[5],
                'last_record': row[6],
                'percentage': 0  # Will be calculated below
            })

        # Calculate percentages
        if total_usage > 0:
            for result in results:
                result['percentage'] = round((result['usage_bytes'] / total_usage) * 100, 2)

        conn.close()

        return jsonify({
            'period_days': days,
            'wifi_ssid': wifi_ssid or 'All networks',
            'total_usage_bytes': total_usage,
            'total_usage_mb': round(total_usage / (1024 * 1024), 2),
            'total_usage_gb': round(total_usage / (1024 * 1024 * 1024), 2),
            'device_usage': results,
            'device_count': len(results),
            'generated_at': datetime.now().isoformat()
        })

    except Exception as e:
        print(f"Error getting usage summary: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/billing', methods=['GET'])
def calculate_billing():
    """Calculate billing based on usage"""
    try:
        # Get parameters
        days = int(request.args.get('days', 30))
        total_bill = float(request.args.get('total_bill', 0))
        wifi_ssid = request.args.get('wifi_ssid', '')

        if total_bill <= 0:
            return jsonify({'error': 'total_bill parameter is required and must be > 0'}), 400

        # Get usage summary
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        start_date = (datetime.now() - timedelta(days=days)).isoformat()

        base_query = '''
            SELECT 
                u.device_id,
                d.device_name,
                d.owner_name,
                SUM(u.total_bytes) as total_usage
            FROM usage_records u
            LEFT JOIN devices d ON u.device_id = d.device_id
            WHERE u.timestamp >= ?
        '''

        params = [start_date]

        if wifi_ssid:
            base_query += ' AND u.wifi_ssid = ?'
            params.append(wifi_ssid)

        base_query += ' GROUP BY u.device_id'

        cursor.execute(base_query, params)

        results = []
        total_usage = 0

        for row in cursor.fetchall():
            usage_bytes = row[3] or 0
            total_usage += usage_bytes

            results.append({
                'device_id': row[0],
                'device_name': row[1] or 'Unknown Device',
                'owner_name': row[2] or 'Unknown Owner',
                'usage_bytes': usage_bytes,
                'usage_gb': round(usage_bytes / (1024 * 1024 * 1024), 2)
            })

        # Calculate bills
        billing_results = []
        total_calculated = 0

        if total_usage > 0:
            for result in results:
                percentage = (result['usage_bytes'] / total_usage) * 100
                bill_amount = (result['usage_bytes'] / total_usage) * total_bill
                total_calculated += bill_amount

                billing_results.append({
                    'device_id': result['device_id'],
                    'device_name': result['device_name'],
                    'owner_name': result['owner_name'],
                    'usage_gb': result['usage_gb'],
                    'usage_percentage': round(percentage, 2),
                    'bill_amount': round(bill_amount, 2)
                })

        conn.close()

        return jsonify({
            'billing_period_days': days,
            'total_bill': total_bill,
            'total_usage_gb': round(total_usage / (1024 * 1024 * 1024), 2),
            'billing_breakdown': billing_results,
            'total_calculated': round(total_calculated, 2),
            'generated_at': datetime.now().isoformat()
        })

    except Exception as e:
        print(f"Error calculating billing: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': 'connected' if os.path.exists(DATABASE_PATH) else 'not found'
    })


@app.route('/', methods=['GET'])
def index():
    """API documentation"""
    return jsonify({
        'name': 'WiFi Usage Tracking API',
        'version': '1.0.0',
        'endpoints': {
            'POST /api/usage': 'Submit usage data from devices',
            'GET /api/devices': 'Get list of registered devices',
            'PUT /api/devices/<device_id>': 'Update device information',
            'GET /api/usage/summary': 'Get usage summary (params: days, wifi_ssid)',
            'GET /api/billing': 'Calculate billing (params: days, total_bill, wifi_ssid)',
            'GET /api/health': 'Health check'
        },
        'authentication': 'Bearer token required for most endpoints'
    })


if __name__ == '__main__':
    # Initialize database
    init_database()

    print("WiFi Usage API Server")
    print("====================")
    print(f"Database: {DATABASE_PATH}")
    print(f"API Key: {API_KEY}")
    print("Change the API_KEY before production use!")
    print("\nStarting server...")

    # Run the Flask app
    app.run(host='0.0.0.0', port=5000, debug=True)