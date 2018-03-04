import os

from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from tethercalc import tethercalc

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ['DATABASE_URL']
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Report(db.Model):
    report_id = db.Column(db.String(16), primary_key=True)
    fight_id = db.Column(db.Integer, primary_key=True)
    results = db.Column(db.JSON)
    friends = db.Column(db.JSON)

@app.route('/', methods=['GET', 'POST'])
def homepage():
    """Simple form for redirecting to a report, no validation"""
    if request.method == 'POST':
        report_id = request.form['report_id']
        fight_id = request.form['fight_id']
        return redirect(url_for('calc', report_id=report_id, fight_id=fight_id))

    return render_template('home.html')

@app.route('/<string:report_id>/<int:fight_id>')
def calc(report_id, fight_id):
    """The actual calculated results view"""
    # Very light validation, more for the db query than for the user
    if len(report_id) != 16:
        return redirect(url_for('homepage'))

    report = Report.query.filter_by(report_id=report_id, fight_id=fight_id).first()

    if report:
        results = report.results
        # These get returned with string keys, so have to massage it some
        friends = {int(k):v for k,v in report.friends.items()}
    else:
        results, friends = tethercalc(report_id, fight_id)
        report = Report(
            report_id=report_id,
            fight_id=fight_id,
            results=results,
            friends=friends)
        db.session.add(report)
        db.session.commit()

    return render_template('calc.html', results=results, friends=friends)
