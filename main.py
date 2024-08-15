import math
import os
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_jwt_extended import JWTManager, jwt_required, create_access_token, get_jwt_identity
from datetime import datetime, timedelta
import requests
from flask_cors import CORS
import logging
import sys


app = Flask(__name__)
CORS(app)

# Configure logging
app.logger.addHandler(logging.StreamHandler(sys.stdout))
app.logger.setLevel(logging.ERROR)

# Database configuration
database_url = os.environ.get('DATABASE_URL', 'sqlite:///solar_forecast.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# JWT configuration
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'default_secret_key')
jwt = JWTManager(app)

# SolCast API configuration
SOLCAST_API_KEY = os.environ.get('SOLCAST_API_KEY', 'default_solcast_api_key')
SOLCAST_BASE_URL = 'https://api.solcast.com.au'

# Database models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)

class Plant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    capacity = db.Column(db.Float, nullable=False)  # in kW
    size = db.Column(db.Float, nullable=False)  # in square meters
    panel_angle = db.Column(db.Float, default=30.0)  # in degrees
    panel_azimuth = db.Column(db.Float, default=180.0)  # in degrees

class Forecast(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plant_id = db.Column(db.Integer, db.ForeignKey('plant.id'), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False)
    ghi = db.Column(db.Float, nullable=False)
    dni = db.Column(db.Float, nullable=False)
    dhi = db.Column(db.Float, nullable=False)
    air_temp = db.Column(db.Float, nullable=False)
    cloud_opacity = db.Column(db.Float, nullable=False)

class PlantPerformance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plant_id = db.Column(db.Integer, db.ForeignKey('plant.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    expected_output = db.Column(db.Float, nullable=False)  # in kWh

def get_solcast_forecast(latitude, longitude):
    url = f"{SOLCAST_BASE_URL}/radiation/forecasts?latitude={latitude}&longitude={longitude}&api_key={SOLCAST_API_KEY}"
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()['forecasts']
    else:
        app.logger.error(f"Failed to fetch SolCast data: {response.status_code}")
        return None

def update_forecast_data():
    plants = Plant.query.all()
    for plant in plants:
        forecast_data = get_solcast_forecast(plant.latitude, plant.longitude)
        if forecast_data:
            for data in forecast_data:
                existing_forecast = Forecast.query.filter_by(
                    plant_id=plant.id,
                    timestamp=datetime.fromisoformat(data['period_end'])
                ).first()

                if existing_forecast:
                    existing_forecast.ghi = data['ghi']
                    existing_forecast.dni = data['dni']
                    existing_forecast.dhi = data['dhi']
                    existing_forecast.air_temp = data['air_temp']
                    existing_forecast.cloud_opacity = data['cloud_opacity']
                else:
                    new_forecast = Forecast(
                        plant_id=plant.id,
                        timestamp=datetime.fromisoformat(data['period_end']),
                        ghi=data['ghi'],
                        dni=data['dni'],
                        dhi=data['dhi'],
                        air_temp=data['air_temp'],
                        cloud_opacity=data['cloud_opacity']
                    )
                    db.session.add(new_forecast)
    
    db.session.commit()

def calculate_plant_performance():
    today = datetime.now().date()
    plants = Plant.query.all()
    for plant in plants:
        forecasts = Forecast.query.filter(
            Forecast.plant_id == plant.id,
            Forecast.timestamp >= today,
            Forecast.timestamp < today + timedelta(days=1)
        ).all()

        total_energy = sum(
            (f.dni * abs(math.sin(math.radians(plant.panel_angle))) * math.cos(math.radians(plant.panel_azimuth)) +
             f.dhi * (1 + math.cos(math.radians(plant.panel_angle))) / 2 +
             f.ghi * 0.2 * (1 - math.cos(math.radians(plant.panel_angle))) / 2) * 
            plant.size * 0.15  # Assuming 15% panel efficiency
            for f in forecasts
        ) / 1000  # Convert to kWh

        performance = PlantPerformance(
            plant_id=plant.id,
            date=today,
            expected_output=total_energy
        )
        db.session.add(performance)
    
    db.session.commit()

@app.route('/login', methods=['POST'])
def login():
    username = request.json.get('username', None)
    password = request.json.get('password', None)
    user = User.query.filter_by(username=username).first()
    if user and user.password == password:  # In production, use proper password hashing
        access_token = create_access_token(identity=username)
        return jsonify(access_token=access_token), 200
    return jsonify({"msg": "Bad username or password"}), 401

@app.route('/forecast/<plant_name>', methods=['GET'])
@jwt_required()
def get_forecast(plant_name):
    current_user = get_jwt_identity()
    user = User.query.filter_by(username=current_user).first()
    if not user:
        return jsonify({"msg": "User not found"}), 404

    plant = Plant.query.filter_by(user_id=user.id, name=plant_name).first()
    if not plant:
        return jsonify({"msg": "Plant not found"}), 404

    forecasts = Forecast.query.filter_by(plant_id=plant.id).order_by(Forecast.timestamp).all()
    forecast_data = [
        {
            "timestamp": f.timestamp.isoformat(),
            "ghi": f.ghi,
            "dni": f.dni,
            "dhi": f.dhi,
            "air_temp": f.air_temp,
            "cloud_opacity": f.cloud_opacity
        } for f in forecasts
    ]

    return jsonify(forecast_data), 200

@app.route('/performance/<plant_name>', methods=['GET'])
@jwt_required()
def get_performance(plant_name):
    current_user = get_jwt_identity()
    user = User.query.filter_by(username=current_user).first()
    if not user:
        return jsonify({"msg": "User not found"}), 404

    plant = Plant.query.filter_by(user_id=user.id, name=plant_name).first()
    if not plant:
        return jsonify({"msg": "Plant not found"}), 404

    performances = PlantPerformance.query.filter_by(plant_id=plant.id).order_by(PlantPerformance.date.desc()).limit(30).all()
    performance_data = [
        {
            "date": p.date.isoformat(),
            "expected_output": p.expected_output
        } for p in performances
    ]

    return jsonify(performance_data), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
