import sqlite3
import os
import hashlib

DB_PATH = os.path.join(os.path.dirname(__file__), "loteadmin.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        rol TEXT NOT NULL DEFAULT 'lectura',
        activo INTEGER NOT NULL DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS proyectos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        ubicacion TEXT,
        departamento TEXT,
        ciudad TEXT,
        lotes_total INTEGER DEFAULT 0,
        superficie_m2 REAL,
        precio_contado REAL,
        precio_financiado REAL,
        entrega_inicial REAL,
        cuotas_max INTEGER DEFAULT 60,
        interes_mora_pct REAL DEFAULT 3.0,
        gastos_mora_pct REAL DEFAULT 1.0,
        estado TEXT DEFAULT 'activo',
        descripcion TEXT,
        creado_en TEXT DEFAULT (datetime('now')),
        creado_por INTEGER REFERENCES usuarios(id)
    );

    CREATE TABLE IF NOT EXISTS lotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proyecto_id INTEGER NOT NULL REFERENCES proyectos(id),
        numero TEXT NOT NULL,
        manzana TEXT,
        superficie_m2 REAL,
        precio_contado REAL,
        precio_financiado REAL,
        estado TEXT DEFAULT 'disponible',
        observaciones TEXT,
        creado_en TEXT DEFAULT (datetime('now')),
        UNIQUE(proyecto_id, numero, manzana)
    );

    CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        cedula TEXT UNIQUE NOT NULL,
        celular TEXT NOT NULL,
        email TEXT,
        direccion TEXT,
        ciudad TEXT,
        departamento TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT (datetime('now')),
        creado_por INTEGER REFERENCES usuarios(id)
    );

    CREATE TABLE IF NOT EXISTS contratos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL REFERENCES clientes(id),
        proyecto_id INTEGER NOT NULL REFERENCES proyectos(id),
        lote_id INTEGER NOT NULL REFERENCES lotes(id),
        precio_total REAL NOT NULL,
        entrega_inicial REAL NOT NULL DEFAULT 0,
        saldo_financiado REAL NOT NULL,
        cantidad_cuotas INTEGER NOT NULL,
        cuota_base REAL NOT NULL,
        fecha_firma TEXT NOT NULL,
        fecha_primer_venc TEXT NOT NULL,
        dia_vencimiento INTEGER NOT NULL DEFAULT 1,
        interes_mora_pct REAL DEFAULT 3.0,
        gastos_mora_pct REAL DEFAULT 1.0,
        revaluacion_pct REAL DEFAULT 5.0,
        estado TEXT DEFAULT 'vigente',
        observaciones TEXT,
        creado_en TEXT DEFAULT (datetime('now')),
        creado_por INTEGER REFERENCES usuarios(id)
    );

    CREATE TABLE IF NOT EXISTS cuotas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrato_id INTEGER NOT NULL REFERENCES contratos(id),
        numero INTEGER NOT NULL,
        fecha_vencimiento TEXT NOT NULL,
        monto_base REAL NOT NULL,
        monto_revaluado REAL NOT NULL,
        mora REAL DEFAULT 0,
        gastos_mora REAL DEFAULT 0,
        total_a_pagar REAL NOT NULL,
        estado TEXT DEFAULT 'pendiente',
        fecha_pago TEXT,
        monto_pagado REAL DEFAULT 0,
        saldo_pendiente REAL,
        observaciones TEXT,
        UNIQUE(contrato_id, numero)
    );

    CREATE TABLE IF NOT EXISTS pagos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contrato_id INTEGER NOT NULL REFERENCES contratos(id),
        cuota_id INTEGER REFERENCES cuotas(id),
        fecha_pago TEXT NOT NULL,
        monto REAL NOT NULL,
        medio TEXT NOT NULL DEFAULT 'efectivo',
        numero_comprobante TEXT,
        observaciones TEXT,
        creado_en TEXT DEFAULT (datetime('now')),
        creado_por INTEGER REFERENCES usuarios(id)
    );

    CREATE TABLE IF NOT EXISTS comunicaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT NOT NULL,
        canal TEXT NOT NULL,
        plantilla_id TEXT,
        cliente_id INTEGER REFERENCES clientes(id),
        contrato_id INTEGER REFERENCES contratos(id),
        mensaje TEXT,
        estado TEXT DEFAULT 'pendiente',
        enviado_en TEXT,
        creado_por INTEGER REFERENCES usuarios(id),
        creado_en TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS plantillas_msg (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        tipo TEXT NOT NULL,
        mensaje_wsp TEXT NOT NULL,
        mensaje_sms TEXT NOT NULL,
        activo INTEGER DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS automatizaciones (
        id TEXT PRIMARY KEY,
        nombre TEXT NOT NULL,
        tipo TEXT NOT NULL,
        plantilla_id TEXT REFERENCES plantillas_msg(id),
        canales TEXT NOT NULL DEFAULT 'whatsapp',
        activo INTEGER DEFAULT 1,
        hora_inicio TEXT DEFAULT '08:00',
        hora_fin TEXT DEFAULT '20:00'
    );

    CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER REFERENCES usuarios(id),
        accion TEXT NOT NULL,
        tabla TEXT,
        registro_id INTEGER,
        detalle TEXT,
        ip TEXT,
        creado_en TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS caja_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        porcentaje REAL NOT NULL,
        tipo TEXT DEFAULT 'egreso'
    );

    CREATE TABLE IF NOT EXISTS caja_egresos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        concepto TEXT NOT NULL,
        monto REAL NOT NULL,
        fecha TEXT NOT NULL,
        descripcion TEXT,
        creado_por INTEGER REFERENCES usuarios(id),
        creado_en TEXT DEFAULT (datetime('now'))
    );
    """)

    # Seed usuario admin por defecto
    c.execute("SELECT id FROM usuarios WHERE email='admin@loteadmin.com'")
    if not c.fetchone():
        c.execute("""
            INSERT INTO usuarios (nombre, email, password_hash, rol)
            VALUES (?, ?, ?, ?)
        """, ('Administrador', 'admin@loteadmin.com', hash_password('admin123'), 'admin'))

    # Seed plantillas
    c.execute("SELECT id FROM plantillas_msg WHERE id='prevencimiento'")
    if not c.fetchone():
        plantillas = [
            ('prevencimiento', 'Recordatorio — 48 hs antes', 'prevencimiento',
             'Hola {nombre_cliente} 👋\n\nLe recordamos que su cuota N°{num_cuota} del {proyecto} - {lote} vence el *{fecha_vencimiento}*.\n\nMonto a pagar: *Gs. {monto_cuota}*\n\nRealice su pago antes del vencimiento para evitar recargos por mora.\n\nConsultas: {telefono_admin}\n_Administración LoteAdmin_',
             'LoteAdmin: Hola {nombre_cliente}, su cuota N°{num_cuota} de {proyecto} vence el {fecha_vencimiento}. Monto: Gs. {monto_cuota}. Pague a tiempo para evitar mora. Info: {telefono_admin}'),
            ('vencimiento', 'Aviso de vencimiento — hoy', 'vencimiento',
             'Estimado/a {nombre_cliente} 📋\n\nLe informamos que HOY vence su cuota N°{num_cuota} del {proyecto} - {lote}.\n\nMonto: *Gs. {monto_cuota}*\n\nRealice el pago hoy para evitar el cálculo de intereses moratorios.\n\nContacto: {telefono_admin}\n_Administración LoteAdmin_',
             'LoteAdmin: {nombre_cliente}, HOY vence su cuota N°{num_cuota} de {proyecto}. Monto: Gs. {monto_cuota}. Pague hoy para evitar mora. Contacto: {telefono_admin}'),
            ('postvencimiento', 'Aviso de mora — 48 hs después', 'postvencimiento',
             'Estimado/a {nombre_cliente} ⚠️\n\nSu cuota N°{num_cuota} del {proyecto} - {lote} se encuentra *vencida*.\n\nCapital: Gs. {monto_cuota}\nMora e intereses: *Gs. {mora_total}*\n*Total actualizado: Gs. {total_deuda}*\n\nRegularice su situación a la brevedad para evitar mayores recargos.\n\nPara coordinar el pago: {telefono_admin}\n_Administración LoteAdmin_',
             'LoteAdmin: {nombre_cliente}, su cuota N°{num_cuota} de {proyecto} está VENCIDA. Capital: Gs. {monto_cuota} + Mora: Gs. {mora_total} = Total: Gs. {total_deuda}. Regularice: {telefono_admin}'),
        ]
        for p in plantillas:
            c.execute("INSERT INTO plantillas_msg (id, nombre, tipo, mensaje_wsp, mensaje_sms) VALUES (?,?,?,?,?)", p)

    # Seed automatizaciones
    c.execute("SELECT id FROM automatizaciones WHERE id='auto1'")
    if not c.fetchone():
        autos = [
            ('auto1', 'Recordatorio 48 hs antes', 'prevencimiento', 'prevencimiento', 'whatsapp'),
            ('auto2', 'Aviso día de vencimiento', 'vencimiento', 'vencimiento', 'whatsapp,sms'),
            ('auto3', 'Aviso mora 48 hs después', 'postvencimiento', 'postvencimiento', 'whatsapp,sms'),
        ]
        for a in autos:
            c.execute("INSERT INTO automatizaciones (id, nombre, tipo, plantilla_id, canales) VALUES (?,?,?,?,?)", a)

    # Seed datos de demo
    c.execute("SELECT id FROM proyectos WHERE nombre='Barrio San José'")
    if not c.fetchone():
        c.execute("""INSERT INTO proyectos (nombre, ubicacion, departamento, ciudad, lotes_total,
            superficie_m2, precio_contado, precio_financiado, entrega_inicial,
            cuotas_max, interes_mora_pct, gastos_mora_pct, estado)
            VALUES ('Barrio San José','Ruta 1 km 18','Central','Luque',
            40,360,25000000,35000000,3000000,60,3.0,1.0,'activo')""")
        proy1 = c.lastrowid

        c.execute("""INSERT INTO proyectos (nombre, ubicacion, departamento, ciudad, lotes_total,
            superficie_m2, precio_contado, precio_financiado, entrega_inicial,
            cuotas_max, interes_mora_pct, gastos_mora_pct, estado)
            VALUES ('Residencial Los Algarrobos','Avda. Mcal. López 2500','Central','San Lorenzo',
            25,250,18000000,26000000,2500000,48,3.0,1.0,'activo')""")
        proy2 = c.lastrowid

        lotes_data = [
            (proy1,'01','A',360,25000000,35000000,'disponible'),
            (proy1,'02','A',360,25000000,35000000,'disponible'),
            (proy1,'03','A',360,25000000,35000000,'disponible'),
            (proy1,'04','B',400,28000000,38000000,'disponible'),
            (proy1,'05','B',400,28000000,38000000,'disponible'),
            (proy2,'01','A',250,18000000,26000000,'disponible'),
            (proy2,'02','A',250,18000000,26000000,'disponible'),
            (proy2,'03','B',250,18000000,26000000,'disponible'),
        ]
        for l in lotes_data:
            c.execute("""INSERT INTO lotes (proyecto_id,numero,manzana,superficie_m2,
                precio_contado,precio_financiado,estado) VALUES (?,?,?,?,?,?,?)""", l)

    # Seed caja config por defecto
    c.execute("SELECT id FROM caja_config LIMIT 1")
    if not c.fetchone():
        configs = [
            ('Propietario del inmueble', 75.0, 'egreso'),
            ('Comisión administradora', 15.0, 'egreso'),
            ('Gastos de mantenimiento', 10.0, 'egreso'),
        ]
        for cfg in configs:
            c.execute("INSERT INTO caja_config (nombre, porcentaje, tipo) VALUES (?,?,?)", cfg)

    conn.commit()
    conn.close()
    print("✓ Base de datos inicializada")

if __name__ == "__main__":
    init_db()
