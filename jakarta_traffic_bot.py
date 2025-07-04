from dotenv import load_dotenv
load_dotenv()
import os
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
from dataclasses import dataclass

import requests
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import schedule
import threading
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

@dataclass
class TrafficData:
    location: str
    duration_in_traffic: int
    duration_normal: int
    timestamp: datetime
    severity: str

class JakartaTrafficBot:
    def __init__(self, telegram_token: str, google_maps_api_key: str):
        self.telegram_token = telegram_token
        self.google_maps_api_key = google_maps_api_key
        self.db_path = "traffic_data.db"
        self.init_database()
        
        # Major Jakarta roads with coordinates
        self.major_roads = {
            "Jalan Sudirman": [
                {"lat": -6.2088, "lng": 106.8456},
                {"lat": -6.2297, "lng": 106.8269}
            ],
            "Jalan Thamrin": [
                {"lat": -6.1944, "lng": 106.8229},
                {"lat": -6.2088, "lng": 106.8456}
            ],
            "Jalan Gatot Subroto": [
                {"lat": -6.2297, "lng": 106.8269},
                {"lat": -6.2615, "lng": 106.7942}
            ],
            "Jakarta-Cikampek Toll": [
                {"lat": -6.1745, "lng": 106.8227},
                {"lat": -6.3139, "lng": 107.1614}
            ],
            "Jalan Rasuna Said": [
                {"lat": -6.2297, "lng": 106.8269},
                {"lat": -6.2383, "lng": 106.8411}
            ]
        }
        
        # Traffic severity thresholds (percentage increase from normal)
        self.severity_thresholds = {
            "normal": 0.15,      # Up to 15% increase
            "moderate": 0.30,    # 15-30% increase
            "heavy": 0.60,       # 30-60% increase
            "severe": 1.0        # 60%+ increase
        }
    
    def init_database(self):
        """Initialize SQLite database for storing traffic data"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS traffic_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL,
                duration_in_traffic INTEGER NOT NULL,
                duration_normal INTEGER NOT NULL,
                timestamp DATETIME NOT NULL,
                severity TEXT NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                user_id INTEGER PRIMARY KEY,
                locations TEXT NOT NULL,
                alert_threshold TEXT DEFAULT 'heavy'
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def get_traffic_data(self, origin: Dict, destination: Dict) -> Optional[TrafficData]:
        """Get traffic data from Google Maps API"""
        try:
            url = "https://maps.googleapis.com/maps/api/directions/json"
            params = {
                "origin": f"{origin['lat']},{origin['lng']}",
                "destination": f"{destination['lat']},{destination['lng']}",
                "departure_time": "now",
                "traffic_model": "best_guess",
                "key": self.google_maps_api_key
            }
            
            response = requests.get(url, params=params)
            data = response.json()
            
            if data["status"] == "OK" and data["routes"]:
                route = data["routes"][0]["legs"][0]
                duration_in_traffic = route["duration_in_traffic"]["value"]
                duration_normal = route["duration"]["value"]
                
                # Calculate severity
                increase_ratio = (duration_in_traffic - duration_normal) / duration_normal
                severity = self.calculate_severity(increase_ratio)
                
                return TrafficData(
                    location=f"{origin['lat']},{origin['lng']}-{destination['lat']},{destination['lng']}",
                    duration_in_traffic=duration_in_traffic,
                    duration_normal=duration_normal,
                    timestamp=datetime.now(),
                    severity=severity
                )
        except Exception as e:
            logger.error(f"Error getting traffic data: {e}")
            return None
    
    def calculate_severity(self, increase_ratio: float) -> str:
        """Calculate traffic severity based on increase ratio"""
        if increase_ratio <= self.severity_thresholds["normal"]:
            return "normal"
        elif increase_ratio <= self.severity_thresholds["moderate"]:
            return "moderate"
        elif increase_ratio <= self.severity_thresholds["heavy"]:
            return "heavy"
        else:
            return "severe"
    
    def store_traffic_data(self, traffic_data: TrafficData):
        """Store traffic data in database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO traffic_history 
            (location, duration_in_traffic, duration_normal, timestamp, severity)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            traffic_data.location,
            traffic_data.duration_in_traffic,
            traffic_data.duration_normal,
            traffic_data.timestamp,
            traffic_data.severity
        ))
        
        conn.commit()
        conn.close()
    
    def get_historical_average(self, location: str, days_back: int = 30) -> Optional[float]:
        """Get historical average traffic duration for a location"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_date = datetime.now() - timedelta(days=days_back)
        
        cursor.execute('''
            SELECT AVG(duration_in_traffic) 
            FROM traffic_history 
            WHERE location = ? AND timestamp > ?
        ''', (location, cutoff_date))
        
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result[0] else None
    
    def is_traffic_unusual(self, current_duration: int, location: str) -> Tuple[bool, str]:
        """Check if current traffic is unusually heavy compared to historical data"""
        historical_avg = self.get_historical_average(location)
        
        if not historical_avg:
            return False, "No historical data available"
        
        increase_ratio = (current_duration - historical_avg) / historical_avg
        
        if increase_ratio > 0.5:  # 50% increase threshold for "unusual"
            return True, f"Traffic is {increase_ratio:.1%} higher than usual"
        
        return False, "Traffic is within normal range"
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        keyboard = [
            [KeyboardButton("📍 Share Location", request_location=True)],
            [KeyboardButton("🚗 Check Major Roads")],
            [KeyboardButton("📊 Traffic Stats")],
            [KeyboardButton("🔔 Subscribe to Alerts")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        welcome_message = """
🚦 Welcome to Jakarta Traffic Monitor Bot!

I can help you with:
• Real-time traffic conditions on major Jakarta roads
• Route planning with current traffic data
• Historical traffic analysis
• Traffic alerts when conditions are unusual

Use the buttons below or try these commands:
/traffic - Check major roads
/route - Plan a route
/stats - View traffic statistics
/subscribe - Set up alerts
        """
        
        await update.message.reply_text(welcome_message, reply_markup=reply_markup)
    
    async def traffic_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /traffic command - show major roads traffic"""
        await update.message.reply_text("🔄 Checking traffic on major Jakarta roads...")
        
        traffic_report = "🚦 **Jakarta Traffic Report**\n\n"
        
        for road_name, coordinates in self.major_roads.items():
            traffic_data = self.get_traffic_data(coordinates[0], coordinates[1])
            
            if traffic_data:
                # Store data for historical analysis
                self.store_traffic_data(traffic_data)
                
                # Check if traffic is unusual
                is_unusual, unusual_msg = self.is_traffic_unusual(
                    traffic_data.duration_in_traffic, 
                    traffic_data.location
                )
                
                duration_mins = traffic_data.duration_in_traffic // 60
                normal_mins = traffic_data.duration_normal // 60
                
                severity_emoji = {
                    "normal": "🟢",
                    "moderate": "🟡", 
                    "heavy": "🟠",
                    "severe": "🔴"
                }
                
                traffic_report += f"{severity_emoji.get(traffic_data.severity, '⚪')} **{road_name}**\n"
                traffic_report += f"Current: {duration_mins} min | Normal: {normal_mins} min\n"
                
                if is_unusual:
                    traffic_report += f"⚠️ {unusual_msg}\n"
                
                traffic_report += "\n"
        
        await update.message.reply_text(traffic_report, parse_mode='Markdown')
    
    async def handle_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle shared location"""
        location = update.message.location
        context.user_data['user_location'] = {
            'lat': location.latitude,
            'lng': location.longitude
        }
        
        await update.message.reply_text(
            f"📍 Location received! ({location.latitude:.4f}, {location.longitude:.4f})\n\n"
            "Now send me your destination address or share another location as destination."
        )
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (destinations)"""
        if 'user_location' not in context.user_data:
            await update.message.reply_text(
                "Please share your location first by clicking the '📍 Share Location' button."
            )
            return
        
        # Geocode the destination
        destination = await self.geocode_address(update.message.text)
        
        if not destination:
            await update.message.reply_text(
                "❌ Could not find that destination. Please try a more specific address."
            )
            return
        
        # Get route information
        origin = context.user_data['user_location']
        traffic_data = self.get_traffic_data(origin, destination)
        
        if traffic_data:
            duration_mins = traffic_data.duration_in_traffic // 60
            normal_mins = traffic_data.duration_normal // 60
            
            is_unusual, unusual_msg = self.is_traffic_unusual(
                traffic_data.duration_in_traffic,
                traffic_data.location
            )
            
            severity_emoji = {
                "normal": "🟢",
                "moderate": "🟡",
                "heavy": "🟠", 
                "severe": "🔴"
            }
            
            response = f"""
🚗 **Route Information**

📍 **Destination:** {update.message.text}
{severity_emoji.get(traffic_data.severity, '⚪')} **Traffic Status:** {traffic_data.severity.title()}

⏱️ **Travel Time:**
• Current (with traffic): {duration_mins} minutes
• Normal conditions: {normal_mins} minutes

{f"⚠️ {unusual_msg}" if is_unusual else "✅ Traffic is normal"}
            """
            
            await update.message.reply_text(response, parse_mode='Markdown')
            
            # Store the data
            self.store_traffic_data(traffic_data)
        else:
            await update.message.reply_text(
                "❌ Could not get traffic data for this route. Please try again."
            )
    
    async def geocode_address(self, address: str) -> Optional[Dict]:
        """Geocode an address using Google Maps API"""
        try:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                "address": f"{address}, Jakarta, Indonesia",
                "key": self.google_maps_api_key
            }
            
            response = requests.get(url, params=params)
            data = response.json()
            
            if data["status"] == "OK" and data["results"]:
                location = data["results"][0]["geometry"]["location"]
                return {"lat": location["lat"], "lng": location["lng"]}
                
        except Exception as e:
            logger.error(f"Geocoding error: {e}")
            
        return None
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get statistics
        cursor.execute('''
            SELECT 
                COUNT(*) as total_records,
                AVG(duration_in_traffic) as avg_duration,
                COUNT(CASE WHEN severity = 'severe' THEN 1 END) as severe_count
            FROM traffic_history 
            WHERE timestamp > datetime('now', '-7 days')
        ''')
        
        stats = cursor.fetchone()
        conn.close()
        
        if stats[0] > 0:
            avg_mins = int(stats[1] / 60) if stats[1] else 0
            severe_percentage = (stats[2] / stats[0]) * 100 if stats[0] > 0 else 0
            
            stats_message = f"""
📊 **Traffic Statistics (Last 7 Days)**

📈 **Total Records:** {stats[0]}
⏱️ **Average Duration:** {avg_mins} minutes
🔴 **Severe Traffic:** {stats[2]} incidents ({severe_percentage:.1f}%)

*Data collected from major Jakarta roads*
            """
        else:
            stats_message = "📊 No traffic data available yet. Check back after some data is collected!"
        
        await update.message.reply_text(stats_message, parse_mode='Markdown')
    
    def collect_traffic_data(self):
        """Scheduled function to collect traffic data"""
        logger.info("Collecting traffic data for major roads...")
        
        for road_name, coordinates in self.major_roads.items():
            traffic_data = self.get_traffic_data(coordinates[0], coordinates[1])
            if traffic_data:
                self.store_traffic_data(traffic_data)
                logger.info(f"Stored traffic data for {road_name}")
    
    def start_scheduler(self):
        """Start the traffic data collection scheduler"""
        schedule.every(15).minutes.do(self.collect_traffic_data)
        
        def run_scheduler():
            while True:
                schedule.run_pending()
                time.sleep(60)
        
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
        logger.info("Traffic data collection scheduler started")
    
    def run(self):
        """Run the bot"""
        # Create application
        application = Application.builder().token(self.telegram_token).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start_command))
        application.add_handler(CommandHandler("traffic", self.traffic_command))
        application.add_handler(CommandHandler("stats", self.stats_command))
        application.add_handler(MessageHandler(filters.LOCATION, self.handle_location))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        
        # Start scheduler
        self.start_scheduler()
        
        # Run the bot
        logger.info("Starting Jakarta Traffic Bot...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    # Configuration
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
    GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_MAPS_API_KEY")
    
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or GOOGLE_MAPS_API_KEY == "YOUR_GOOGLE_MAPS_API_KEY":
        print("❌ Please set your TELEGRAM_TOKEN and GOOGLE_MAPS_API_KEY environment variables")
        print("   export TELEGRAM_TOKEN='your_bot_token'")
        print("   export GOOGLE_MAPS_API_KEY='your_google_maps_api_key'")
        exit(1)
    
    # Create and run bot
    bot = JakartaTrafficBot(TELEGRAM_TOKEN, GOOGLE_MAPS_API_KEY)
    bot.run()