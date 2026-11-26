import subprocess, requests, sqlite3
from fastapi import FastAPI
api = FastAPI()
@api.get("/cmd")
def cmd(h: str):
    return subprocess.run("ping -c1 " + h, shell=True)  # V:CMDI
@api.get("/sql")
def sql(n: str):
    return sqlite3.connect("x").execute(f"SELECT * FROM t WHERE n='{n}'").fetchall()  # V:SQLI
@api.get("/path")
def path(f: str):
    return open("/data/" + f).read()  # V:PATH
@api.get("/ssrf")
def ssrf(u: str):
    return requests.get(u).text  # V:SSRF
@api.get("/eval")
def ev(e: str):
    return eval(e)  # V:EVAL
