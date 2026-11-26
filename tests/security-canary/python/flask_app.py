import subprocess, pickle, hashlib, sqlite3, requests
from flask import Flask, request
app = Flask(__name__)
@app.route("/cmd")
def cmd():
    return subprocess.check_output("ping -c1 " + request.args["h"], shell=True)  # V:CMDI
@app.route("/sql")
def sql():
    con = sqlite3.connect("x")
    return str(con.execute("SELECT * FROM t WHERE n='%s'" % request.args["n"]).fetchall())  # V:SQLI
@app.route("/path")
def path():
    return open("/data/" + request.args["f"]).read()  # V:PATH
@app.route("/ssrf")
def ssrf():
    return requests.get(request.args["u"]).text  # V:SSRF
@app.route("/deser")
def deser():
    return str(pickle.loads(request.data))  # V:DESER
@app.route("/eval")
def ev():
    return str(eval(request.args["e"]))  # V:EVAL
@app.route("/xss")
def xss():
    return "<h1>" + request.args["n"] + "</h1>"  # V:XSS
def weak(p):
    return hashlib.md5(p.encode()).hexdigest()  # V:CRYPTO
