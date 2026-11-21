import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS customer_contract(id TEXT PRIMARY KEY,customer TEXT NOT NULL,order_id TEXT NOT NULL,shade TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,threshold REAL NOT NULL,signed_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_revision(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,version INTEGER NOT NULL,at TEXT NOT NULL,lot TEXT NOT NULL,substitute_of TEXT,approved_by TEXT,withdrawn INTEGER NOT NULL,qty_kg REAL NOT NULL,shade TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_decision(order_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,recipe_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dye_lot(id TEXT PRIMARY KEY,shade TEXT NOT NULL,available_kg REAL NOT NULL,expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vat(id TEXT PRIMARY KEY,capacity_kg REAL NOT NULL,previous_shade TEXT NOT NULL,cleaned_at TEXT);
CREATE TABLE IF NOT EXISTS reservation(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,vat_id TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,qty_kg REAL NOT NULL,priority INTEGER NOT NULL,at TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS colorimeter(id TEXT PRIMARY KEY,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,operator TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shade_sample(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,instrument_id TEXT NOT NULL,at TEXT NOT NULL,delta_e REAL NOT NULL,replicate INTEGER NOT NULL,kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shade_release(order_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,recipe_id TEXT,reservation_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rework_cost(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,dye_kg REAL NOT NULL,reason TEXT NOT NULL,approved_by TEXT);
CREATE TABLE IF NOT EXISTS rework_liability(cost_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,liable_party TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS future_recipe_permission(order_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,shade_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,order_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def history(db,subsystem,order_id,state,reason,source_id=None):
    db.execute('INSERT INTO decision_history(subsystem,order_id,state,reason,source_id) VALUES(?,?,?,?,?)',(subsystem,order_id,state,reason,source_id))
