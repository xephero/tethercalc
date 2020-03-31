from datetime import datetime
import os
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, redirect, send_from_directory, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from tethercalc import tethercalc, get_last_fight_id, TetherCalcException

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

LAST_CALC_DATE = datetime.fromtimestamp(1563736200)

class Report(db.Model):
    report_id = db.Column(db.String(16), primary_key=True)
    fight_id = db.Column(db.Integer, primary_key=True)
    results = db.Column(db.JSON)
    friends = db.Column(db.JSON)
    enc_name = db.Column(db.String(64))
    enc_time = db.Column(db.String(9))
    enc_kill = db.Column(db.Boolean)
    computed = db.Column(db.DateTime, server_default=db.func.now())

class Count(db.Model):
    count_id = db.Column(db.Integer, primary_key=True)
    total_reports = db.Column(db.Integer)

def decompose_url(url):
    parts = urlparse(url)

    report_id = [segment for segment in parts.path.split('/') if segment][-1]
    try:
        fight_id = parse_qs(parts.fragment)['fight'][0]
    except KeyError:
        raise TetherCalcException("Fight ID is required. Select a fight first")

    if fight_id == 'last':
        fight_id = get_last_fight_id(report_id)

    fight_id = int(fight_id)

    return report_id, fight_id

def increment_count(db):
    count = Count.query.get(1)
    count.total_reports = count.total_reports + 1
    db.session.commit()

def prune_reports(db):
    if Report.query.count() > 9500:
        # Get the computed time of the 500th report
        delete_before = Report.query.order_by('computed').offset(500).first().computed

        # Delete reports before that
        Report.query.filter(Report.computed < delete_before).delete()
        db.session.commit()

@app.route('/', methods=['GET', 'POST'])
def homepage():
    """Simple form for redirecting to a report, no validation"""
    if request.method == 'POST':
        report_url = request.form['report_url']
        try:
            report_id, fight_id = decompose_url(report_url)
        except TetherCalcException as exception:
            return render_template('error.html', exception=exception)

        return redirect(url_for('calc', report_id=report_id, fight_id=fight_id))

    return render_template('home.html')

@app.route('/about')
def about():
    count = Count.query.get(1)

    return render_template('about.html', report_count=count.total_reports)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/png')

@app.route('/<string:report_id>/<int:fight_id>')
def calc(report_id, fight_id):
    """The actual calculated results view"""
    # Very light validation, more for the db query than for the user
    if len(report_id) != 16:
        return redirect(url_for('homepage'))

    report = Report.query.filter_by(report_id=report_id, fight_id=fight_id).first()

    if report:
        # Recompute if no computed timestamp
        if not report.computed or report.computed < LAST_CALC_DATE:
            try:
                results, friends, encounter_info = tethercalc(report_id, fight_id)
            except TetherCalcException as exception:
                return render_template('error.html', exception=exception)

            report.results = results
            report.friends = friends
            report.enc_name = encounter_info['enc_name']
            report.enc_time = encounter_info['enc_time']
            report.enc_kill = encounter_info['enc_kill']
            report.computed = datetime.now()

            db.session.commit()

        # These get returned with string keys, so have to massage it some
        friends = {int(k):v for k,v in report.friends.items()}

    else:
        try:
            results, friends, encounter_info = tethercalc(report_id, fight_id)
        except TetherCalcException as exception:
            return render_template('error.html', exception=exception)
        report = Report(
            report_id=report_id,
            fight_id=fight_id,
            results=results,
            friends=friends,
            **encounter_info
            )
        try:
            # Add the report
            db.session.add(report)
            db.session.commit()

            # Increment count
            increment_count(db)

            # Make sure we're not over limit
            prune_reports(db)

        except IntegrityError as exception:
            # This was likely added while tethercalc was running,
            # in which case we don't need to do anything besides redirect
            pass

    return render_template('calc.html', report=report, friends=friends)
