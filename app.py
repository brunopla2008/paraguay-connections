import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, request, jsonify
from datetime import datetime, date, timedelta
import hashlib, json

from db import get_db, init_db, hash_password
from auth import crear_token, requiere_auth
from services import (
    generar_plan_cuotas, actualizar_mora_contrato, resumen_contrato,
    estado_contrato, calcular_cuota_revaluada, destinatarios_comunicacion,
    fill_plantilla
)

app = Flask(__name__)

@app.route('/')
def serve_frontend():
    import os
    frontend_path = os.path.join(os.path.dirname(__file__), 'frontend.html')
    with open(frontend_path, 'r', encoding='utf-8') as f:
        return f.read(), 200, {'Content-Type': 'text/html; charset=utf-8'}

def row_to_dict(row):
    if row is None:
        return None
    return dict(row)

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ─── CORS manual ──────────────────────────────────────────────────────────────
@app.after_request
def cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,PATCH,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options(_=None, path=None):
    return '', 204

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = data.get('email','').strip()
    password = data.get('password','')
    if not email or not password:
        return jsonify({'error': 'Email y contraseña requeridos'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM usuarios WHERE email=? AND activo=1", (email,))
    user = c.fetchone()
    conn.close()
    if not user or user['password_hash'] != hash_password(password):
        return jsonify({'error': 'Credenciales incorrectas'}), 401
    token = crear_token(user['id'], user['rol'], user['nombre'])
    return jsonify({'token': token, 'usuario': {
        'id': user['id'], 'nombre': user['nombre'],
        'email': user['email'], 'rol': user['rol']
    }})

@app.route('/api/auth/me', methods=['GET'])
@requiere_auth()
def me():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nombre, email, rol, creado_en FROM usuarios WHERE id=?", (request.usuario['sub'],))
    u = c.fetchone()
    conn.close()
    return jsonify(row_to_dict(u))

# ─── DASHBOARD ────────────────────────────────────────────────────────────────
@app.route('/api/dashboard', methods=['GET'])
@requiere_auth()
def dashboard():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) as n, COALESCE(SUM(precio_total),0) as total FROM contratos WHERE estado NOT IN ('rescindido')")
    contratos = c.fetchone()

    c.execute("SELECT COALESCE(SUM(monto),0) as total FROM pagos")
    cobrado = c.fetchone()['total']

    c.execute("SELECT estado, COUNT(*) as n FROM lotes GROUP BY estado")
    lotes_estados = {r['estado']: r['n'] for r in c.fetchall()}

    c.execute("SELECT COUNT(*) as n FROM clientes")
    clientes = c.fetchone()['n']

    c.execute("""SELECT COUNT(*) as n, COALESCE(SUM(mora + gastos_mora),0) as total_mora
        FROM cuotas WHERE estado='vencida'""")
    mora_row = c.fetchone()

    hoy = date.today()
    en7 = (hoy + timedelta(days=7)).isoformat()
    c.execute("""SELECT cu.*, cl.nombre as cliente_nombre, p.nombre as proyecto_nombre
        FROM cuotas cu
        JOIN contratos co ON co.id = cu.contrato_id
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        WHERE cu.estado='pendiente' AND cu.fecha_vencimiento <= ?
        ORDER BY cu.fecha_vencimiento ASC LIMIT 10""", (en7,))
    proximos = rows_to_list(c.fetchall())

    c.execute("""SELECT co.id, cl.nombre, COUNT(cu.id) as cuotas_vencidas,
        COALESCE(SUM(cu.mora + cu.gastos_mora),0) as mora_total
        FROM contratos co
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN cuotas cu ON cu.contrato_id = co.id AND cu.estado='vencida'
        GROUP BY co.id HAVING cuotas_vencidas >= 3
        ORDER BY cuotas_vencidas DESC""")
    criticos = rows_to_list(c.fetchall())

    conn.close()
    return jsonify({
        'total_vendido': round(contratos['total']),
        'total_cobrado': round(cobrado),
        'total_mora': round(mora_row['total_mora']),
        'cuotas_vencidas': mora_row['n'],
        'total_clientes': clientes,
        'total_contratos': contratos['n'],
        'lotes': lotes_estados,
        'proximos_vencimientos': proximos,
        'contratos_criticos': criticos,
    })

# ─── PROYECTOS ────────────────────────────────────────────────────────────────
@app.route('/api/proyectos', methods=['GET'])
@requiere_auth()
def get_proyectos():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT p.*,
        COUNT(DISTINCT l.id) as lotes_registrados,
        SUM(CASE WHEN l.estado='disponible' THEN 1 ELSE 0 END) as lotes_disponibles,
        SUM(CASE WHEN l.estado='vendido' THEN 1 ELSE 0 END) as lotes_vendidos,
        SUM(CASE WHEN l.estado='en_mora' THEN 1 ELSE 0 END) as lotes_en_mora
        FROM proyectos p LEFT JOIN lotes l ON l.proyecto_id = p.id
        GROUP BY p.id ORDER BY p.creado_en DESC""")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/proyectos/<int:id>', methods=['GET'])
@requiere_auth()
def get_proyecto(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM proyectos WHERE id=?", (id,))
    p = row_to_dict(c.fetchone())
    if not p:
        conn.close()
        return jsonify({'error': 'Proyecto no encontrado'}), 404
    c.execute("SELECT * FROM lotes WHERE proyecto_id=? ORDER BY manzana, numero", (id,))
    p['lotes'] = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(p)

@app.route('/api/proyectos', methods=['POST'])
@requiere_auth(roles=['admin'])
def crear_proyecto():
    d = request.get_json() or {}
    required = ['nombre', 'precio_financiado', 'cuotas_max']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO proyectos (nombre, ubicacion, departamento, ciudad,
        lotes_total, superficie_m2, precio_contado, precio_financiado,
        entrega_inicial, cuotas_max, interes_mora_pct, gastos_mora_pct,
        estado, descripcion, creado_por)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        d['nombre'], d.get('ubicacion'), d.get('departamento'), d.get('ciudad'),
        d.get('lotes_total', 0), d.get('superficie_m2'),
        d.get('precio_contado'), d['precio_financiado'],
        d.get('entrega_inicial', 0), d['cuotas_max'],
        d.get('interes_mora_pct', 3.0), d.get('gastos_mora_pct', 1.0),
        d.get('estado', 'activo'), d.get('descripcion'),
        request.usuario['sub']
    ))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': new_id, 'mensaje': 'Proyecto creado correctamente'}), 201

@app.route('/api/proyectos/<int:id>', methods=['PUT'])
@requiere_auth(roles=['admin'])
def actualizar_proyecto(id):
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM proyectos WHERE id=?", (id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Proyecto no encontrado'}), 404
    campos = ['nombre','ubicacion','departamento','ciudad','lotes_total',
              'superficie_m2','precio_contado','precio_financiado',
              'entrega_inicial','cuotas_max','interes_mora_pct',
              'gastos_mora_pct','estado','descripcion']
    sets = [f"{k}=?" for k in campos if k in d]
    vals = [d[k] for k in campos if k in d]
    if sets:
        c.execute(f"UPDATE proyectos SET {','.join(sets)} WHERE id=?", vals + [id])
        conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Proyecto actualizado'})

@app.route('/api/proyectos/<int:id>', methods=['DELETE'])
@requiere_auth(roles=['admin'])
def eliminar_proyecto(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM contratos co JOIN lotes l ON l.id=co.lote_id WHERE l.proyecto_id=? AND co.estado NOT IN ('rescindido','cancelado')", (id,))
    if c.fetchone()['n'] > 0:
        conn.close()
        return jsonify({'error': 'No se puede eliminar: el proyecto tiene contratos activos'}), 400
    c.execute("DELETE FROM lotes WHERE proyecto_id=?", (id,))
    c.execute("DELETE FROM proyectos WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Proyecto eliminado'})

# ─── LOTES ────────────────────────────────────────────────────────────────────
@app.route('/api/lotes', methods=['GET'])
@requiere_auth()
def get_lotes():
    conn = get_db()
    c = conn.cursor()
    proyecto_id = request.args.get('proyecto_id')
    estado = request.args.get('estado')
    query = """SELECT l.*, p.nombre as proyecto_nombre,
        cl.nombre as cliente_nombre, cl.id as cliente_id_ref
        FROM lotes l
        JOIN proyectos p ON p.id = l.proyecto_id
        LEFT JOIN contratos co ON co.lote_id = l.id AND co.estado NOT IN ('rescindido','cancelado')
        LEFT JOIN clientes cl ON cl.id = co.cliente_id"""
    params = []
    wheres = []
    if proyecto_id:
        wheres.append("l.proyecto_id=?")
        params.append(proyecto_id)
    if estado:
        wheres.append("l.estado=?")
        params.append(estado)
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += " ORDER BY l.manzana, l.numero"
    c.execute(query, params)
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/lotes', methods=['POST'])
@requiere_auth(roles=['admin'])
def crear_lote():
    d = request.get_json() or {}
    required = ['proyecto_id', 'numero', 'manzana']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""INSERT INTO lotes (proyecto_id, numero, manzana, superficie_m2,
            precio_contado, precio_financiado, estado, observaciones)
            VALUES (?,?,?,?,?,?,?,?)""", (
            d['proyecto_id'], d['numero'], d['manzana'],
            d.get('superficie_m2'), d.get('precio_contado'),
            d.get('precio_financiado'), d.get('estado','disponible'),
            d.get('observaciones')
        ))
        new_id = c.lastrowid
        conn.commit()
        conn.close()
        return jsonify({'id': new_id, 'mensaje': 'Lote creado'}), 201
    except Exception as e:
        conn.close()
        return jsonify({'error': f'El lote ya existe o datos inválidos: {str(e)}'}), 400

# ─── CLIENTES ─────────────────────────────────────────────────────────────────
@app.route('/api/clientes', methods=['GET'])
@requiere_auth()
def get_clientes():
    conn = get_db()
    c = conn.cursor()
    q = request.args.get('q', '')
    query = """SELECT cl.*,
        COUNT(co.id) as total_contratos,
        SUM(CASE WHEN co.estado='en_mora' THEN 1 ELSE 0 END) as contratos_en_mora
        FROM clientes cl
        LEFT JOIN contratos co ON co.cliente_id = cl.id
        WHERE (cl.nombre LIKE ? OR cl.cedula LIKE ? OR cl.celular LIKE ?)
        GROUP BY cl.id ORDER BY cl.nombre"""
    like = f'%{q}%'
    c.execute(query, (like, like, like))
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/clientes/<int:id>', methods=['GET'])
@requiere_auth()
def get_cliente(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM clientes WHERE id=?", (id,))
    cliente = row_to_dict(c.fetchone())
    if not cliente:
        conn.close()
        return jsonify({'error': 'Cliente no encontrado'}), 404
    c.execute("""SELECT co.*, p.nombre as proyecto_nombre,
        l.numero as lote_numero, l.manzana
        FROM contratos co
        JOIN proyectos p ON p.id = co.proyecto_id
        JOIN lotes l ON l.id = co.lote_id
        WHERE co.cliente_id=? ORDER BY co.fecha_firma DESC""", (id,))
    cliente['contratos'] = rows_to_list(c.fetchall())
    c.execute("""SELECT pa.*, cu.numero as cuota_numero
        FROM pagos pa LEFT JOIN cuotas cu ON cu.id = pa.cuota_id
        WHERE pa.contrato_id IN (SELECT id FROM contratos WHERE cliente_id=?)
        ORDER BY pa.fecha_pago DESC LIMIT 20""", (id,))
    cliente['pagos'] = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(cliente)

@app.route('/api/clientes', methods=['POST'])
@requiere_auth(roles=['admin','vendedor'])
def crear_cliente():
    d = request.get_json() or {}
    required = ['nombre', 'cedula', 'celular']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM clientes WHERE cedula=?", (d['cedula'],))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'Ya existe un cliente con esa cédula'}), 400
    c.execute("""INSERT INTO clientes (nombre, cedula, celular, email,
        direccion, ciudad, departamento, observaciones, creado_por)
        VALUES (?,?,?,?,?,?,?,?,?)""", (
        d['nombre'], d['cedula'], d['celular'],
        d.get('email'), d.get('direccion'), d.get('ciudad'),
        d.get('departamento'), d.get('observaciones'),
        request.usuario['sub']
    ))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': new_id, 'mensaje': 'Cliente registrado correctamente'}), 201

@app.route('/api/clientes/<int:id>', methods=['PUT'])
@requiere_auth(roles=['admin','vendedor'])
def actualizar_cliente(id):
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    campos = ['nombre','celular','email','direccion','ciudad','departamento','observaciones']
    sets = [f"{k}=?" for k in campos if k in d]
    vals = [d[k] for k in campos if k in d]
    if sets:
        c.execute(f"UPDATE clientes SET {','.join(sets)} WHERE id=?", vals + [id])
        conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Cliente actualizado'})

# ─── CONTRATOS ────────────────────────────────────────────────────────────────
@app.route('/api/contratos', methods=['GET'])
@requiere_auth()
def get_contratos():
    conn = get_db()
    c = conn.cursor()
    estado = request.args.get('estado')
    query = """SELECT co.*, cl.nombre as cliente_nombre, cl.cedula,
        p.nombre as proyecto_nombre, l.numero as lote_numero, l.manzana
        FROM contratos co
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        JOIN lotes l ON l.id = co.lote_id"""
    params = []
    if estado:
        query += " WHERE co.estado=?"
        params.append(estado)
    query += " ORDER BY co.creado_en DESC"
    c.execute(query, params)
    contratos = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(contratos)

@app.route('/api/contratos/<int:id>', methods=['GET'])
@requiere_auth()
def get_contrato(id):
    actualizar_mora_contrato(id)
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT co.*, cl.nombre as cliente_nombre, cl.cedula, cl.celular,
        p.nombre as proyecto_nombre, l.numero as lote_numero, l.manzana
        FROM contratos co
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        JOIN lotes l ON l.id = co.lote_id
        WHERE co.id=?""", (id,))
    contrato = row_to_dict(c.fetchone())
    if not contrato:
        conn.close()
        return jsonify({'error': 'Contrato no encontrado'}), 404
    c.execute("SELECT * FROM cuotas WHERE contrato_id=? ORDER BY numero", (id,))
    contrato['cuotas'] = rows_to_list(c.fetchall())
    contrato['resumen'] = resumen_contrato(id)
    conn.close()
    return jsonify(contrato)

@app.route('/api/contratos', methods=['POST'])
@requiere_auth(roles=['admin','vendedor'])
def crear_contrato():
    d = request.get_json() or {}
    required = ['cliente_id','proyecto_id','lote_id','precio_total',
                'cantidad_cuotas','fecha_firma','fecha_primer_venc']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    if int(d['cantidad_cuotas']) > 130:
        return jsonify({'error': 'Máximo 130 cuotas'}), 400

    conn = get_db()
    c = conn.cursor()

    # Verificar lote disponible
    c.execute("SELECT estado FROM lotes WHERE id=?", (d['lote_id'],))
    lote = c.fetchone()
    if not lote or lote['estado'] not in ('disponible', 'reservado'):
        conn.close()
        return jsonify({'error': 'El lote no está disponible'}), 400

    entrega = float(d.get('entrega_inicial', 0))
    precio = float(d['precio_total'])
    saldo = precio - entrega
    cuotas = int(d['cantidad_cuotas'])
    cuota_base = round(saldo / cuotas)

    c.execute("""INSERT INTO contratos (cliente_id, proyecto_id, lote_id,
        precio_total, entrega_inicial, saldo_financiado, cantidad_cuotas,
        cuota_base, fecha_firma, fecha_primer_venc, dia_vencimiento,
        interes_mora_pct, gastos_mora_pct, revaluacion_pct,
        estado, observaciones, creado_por)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        d['cliente_id'], d['proyecto_id'], d['lote_id'],
        precio, entrega, saldo, cuotas, cuota_base,
        d['fecha_firma'], d['fecha_primer_venc'],
        d.get('dia_vencimiento', 1),
        d.get('interes_mora_pct', 3.0),
        d.get('gastos_mora_pct', 1.0),
        d.get('revaluacion_pct', 5.0),
        'vigente', d.get('observaciones'),
        request.usuario['sub']
    ))
    contrato_id = c.lastrowid

    # Cambiar estado del lote
    c.execute("UPDATE lotes SET estado='vendido' WHERE id=?", (d['lote_id'],))
    conn.commit()
    conn.close()

    # Generar plan de cuotas
    generar_plan_cuotas(contrato_id)

    return jsonify({
        'id': contrato_id,
        'cuota_base': cuota_base,
        'saldo_financiado': saldo,
        'mensaje': f'Contrato creado con {cuotas} cuotas generadas automáticamente'
    }), 201

@app.route('/api/contratos/<int:id>/estado', methods=['PATCH'])
@requiere_auth(roles=['admin'])
def cambiar_estado_contrato(id):
    d = request.get_json() or {}
    nuevo_estado = d.get('estado')
    estados_validos = ['vigente','en_mora','cancelado','rescindido','finalizado']
    if nuevo_estado not in estados_validos:
        return jsonify({'error': 'Estado inválido'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE contratos SET estado=? WHERE id=?", (nuevo_estado, id))
    if nuevo_estado == 'rescindido':
        c.execute("SELECT lote_id FROM contratos WHERE id=?", (id,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE lotes SET estado='recuperado' WHERE id=?", (row['lote_id'],))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': f'Contrato actualizado a {nuevo_estado}'})

# ─── CUOTAS ───────────────────────────────────────────────────────────────────
@app.route('/api/cuotas', methods=['GET'])
@requiere_auth()
def get_cuotas():
    conn = get_db()
    c = conn.cursor()
    estado = request.args.get('estado')
    contrato_id = request.args.get('contrato_id')
    query = """SELECT cu.*, cl.nombre as cliente_nombre, p.nombre as proyecto_nombre
        FROM cuotas cu
        JOIN contratos co ON co.id = cu.contrato_id
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id WHERE 1=1"""
    params = []
    if estado:
        query += " AND cu.estado=?"
        params.append(estado)
    if contrato_id:
        query += " AND cu.contrato_id=?"
        params.append(contrato_id)
    query += " ORDER BY cu.fecha_vencimiento ASC"
    c.execute(query, params)
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/cuotas/vencidas', methods=['GET'])
@requiere_auth()
def cuotas_vencidas():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT cu.*, cl.nombre as cliente_nombre, cl.celular,
        p.nombre as proyecto_nombre, l.numero as lote_numero, l.manzana
        FROM cuotas cu
        JOIN contratos co ON co.id = cu.contrato_id
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        JOIN lotes l ON l.id = co.lote_id
        WHERE cu.estado='vencida'
        ORDER BY cu.fecha_vencimiento ASC""")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/cuotas/proximas', methods=['GET'])
@requiere_auth()
def cuotas_proximas():
    dias = int(request.args.get('dias', 7))
    hoy = date.today()
    limite = (hoy + timedelta(days=dias)).isoformat()
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT cu.*, cl.nombre as cliente_nombre, cl.celular,
        p.nombre as proyecto_nombre
        FROM cuotas cu
        JOIN contratos co ON co.id = cu.contrato_id
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        WHERE cu.estado='pendiente' AND cu.fecha_vencimiento <= ?
        ORDER BY cu.fecha_vencimiento ASC""", (limite,))
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

# ─── PAGOS ────────────────────────────────────────────────────────────────────
@app.route('/api/pagos', methods=['GET'])
@requiere_auth()
def get_pagos():
    conn = get_db()
    c = conn.cursor()
    contrato_id = request.args.get('contrato_id')
    query = """SELECT pa.*, cl.nombre as cliente_nombre,
        p.nombre as proyecto_nombre, cu.numero as cuota_numero
        FROM pagos pa
        JOIN contratos co ON co.id = pa.contrato_id
        JOIN clientes cl ON cl.id = co.cliente_id
        JOIN proyectos p ON p.id = co.proyecto_id
        LEFT JOIN cuotas cu ON cu.id = pa.cuota_id WHERE 1=1"""
    params = []
    if contrato_id:
        query += " AND pa.contrato_id=?"
        params.append(contrato_id)
    query += " ORDER BY pa.fecha_pago DESC LIMIT 100"
    c.execute(query, params)
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/pagos', methods=['POST'])
@requiere_auth(roles=['admin','cobrador'])
def registrar_pago():
    d = request.get_json() or {}
    required = ['contrato_id', 'cuota_id', 'fecha_pago', 'monto']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM cuotas WHERE id=?", (d['cuota_id'],))
    cuota = c.fetchone()
    if not cuota:
        conn.close()
        return jsonify({'error': 'Cuota no encontrada'}), 404

    monto = float(d['monto'])
    nuevo_pagado = (cuota['monto_pagado'] or 0) + monto
    saldo = max(0, cuota['total_a_pagar'] - nuevo_pagado)
    nuevo_estado = 'pagada' if saldo <= 0 else 'parcial'

    c.execute("""UPDATE cuotas SET monto_pagado=?, saldo_pendiente=?,
        estado=?, fecha_pago=? WHERE id=?""",
        (nuevo_pagado, saldo, nuevo_estado, d['fecha_pago'], cuota['id']))

    c.execute("""INSERT INTO pagos (contrato_id, cuota_id, fecha_pago,
        monto, medio, numero_comprobante, observaciones, creado_por)
        VALUES (?,?,?,?,?,?,?,?)""", (
        d['contrato_id'], d['cuota_id'], d['fecha_pago'],
        monto, d.get('medio','efectivo'),
        d.get('numero_comprobante'), d.get('observaciones'),
        request.usuario['sub']
    ))
    pago_id = c.lastrowid

    # Actualizar estado del contrato
    nuevo_estado_contrato = estado_contrato(d['contrato_id'])
    c.execute("UPDATE contratos SET estado=? WHERE id=?",
              (nuevo_estado_contrato, d['contrato_id']))

    conn.commit()
    conn.close()
    return jsonify({
        'id': pago_id,
        'saldo_pendiente': round(saldo),
        'estado_cuota': nuevo_estado,
        'mensaje': 'Pago registrado correctamente'
    }), 201

# ─── REPORTES ─────────────────────────────────────────────────────────────────
@app.route('/api/reportes/resumen', methods=['GET'])
@requiere_auth()
def reporte_resumen():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) as n, COALESCE(SUM(precio_total),0) as total FROM contratos WHERE estado NOT IN ('rescindido')")
    contratos = c.fetchone()

    c.execute("SELECT COALESCE(SUM(monto),0) as t FROM pagos")
    cobrado = c.fetchone()['t']

    c.execute("SELECT estado, COUNT(*) as n FROM lotes GROUP BY estado")
    lotes = {r['estado']: r['n'] for r in c.fetchall()}

    c.execute("SELECT COALESCE(SUM(mora+gastos_mora),0) as t FROM cuotas WHERE estado='vencida'")
    mora = c.fetchone()['t']

    c.execute("SELECT medio, COUNT(*) as n, COALESCE(SUM(monto),0) as total FROM pagos GROUP BY medio")
    por_medio = rows_to_list(c.fetchall())

    c.execute("""SELECT strftime('%Y-%m', fecha_pago) as mes,
        COALESCE(SUM(monto),0) as total, COUNT(*) as pagos
        FROM pagos GROUP BY mes ORDER BY mes DESC LIMIT 12""")
    por_mes = rows_to_list(c.fetchall())

    conn.close()
    return jsonify({
        'total_vendido': round(contratos['total']),
        'total_contratos': contratos['n'],
        'total_cobrado': round(cobrado),
        'mora_acumulada': round(mora),
        'lotes_por_estado': lotes,
        'pagos_por_medio': por_medio,
        'cobros_por_mes': por_mes,
    })

@app.route('/api/reportes/mora', methods=['GET'])
@requiere_auth()
def reporte_mora():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT co.id as contrato_id, cl.nombre, cl.cedula, cl.celular,
        p.nombre as proyecto, l.numero as lote, l.manzana,
        COUNT(cu.id) as cuotas_vencidas,
        COALESCE(SUM(cu.monto_revaluado),0) as capital_vencido,
        COALESCE(SUM(cu.mora+cu.gastos_mora),0) as mora_total,
        COALESCE(SUM(cu.total_a_pagar - COALESCE(cu.monto_pagado,0)),0) as deuda_total
        FROM contratos co
        JOIN clientes cl ON cl.id=co.cliente_id
        JOIN proyectos p ON p.id=co.proyecto_id
        JOIN lotes l ON l.id=co.lote_id
        JOIN cuotas cu ON cu.contrato_id=co.id AND cu.estado='vencida'
        GROUP BY co.id ORDER BY cuotas_vencidas DESC""")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

# ─── COMUNICACIÓN ─────────────────────────────────────────────────────────────
@app.route('/api/comunicacion/plantillas', methods=['GET'])
@requiere_auth()
def get_plantillas():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM plantillas_msg ORDER BY tipo")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/comunicacion/plantillas/<id>', methods=['PUT'])
@requiere_auth(roles=['admin'])
def actualizar_plantilla(id):
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    campos = ['nombre','mensaje_wsp','mensaje_sms','activo']
    sets = [f"{k}=?" for k in campos if k in d]
    vals = [d[k] for k in campos if k in d]
    if sets:
        c.execute(f"UPDATE plantillas_msg SET {','.join(sets)} WHERE id=?", vals + [id])
        conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Plantilla actualizada'})

@app.route('/api/comunicacion/destinatarios/<tipo>', methods=['GET'])
@requiere_auth()
def get_destinatarios(tipo):
    if tipo not in ('prevencimiento', 'vencimiento', 'postvencimiento'):
        return jsonify({'error': 'Tipo inválido'}), 400
    resultado = destinatarios_comunicacion(tipo)
    return jsonify({'tipo': tipo, 'total': len(resultado), 'destinatarios': resultado})

@app.route('/api/comunicacion/preview', methods=['POST'])
@requiere_auth()
def preview_mensaje():
    d = request.get_json() or {}
    tipo = d.get('tipo')
    canal = d.get('canal', 'whatsapp')
    if not tipo:
        return jsonify({'error': 'tipo requerido'}), 400

    conn = get_db()
    c = conn.cursor()
    plantilla_map = {'prevencimiento':'prevencimiento','vencimiento':'vencimiento','postvencimiento':'postvencimiento'}
    c.execute("SELECT * FROM plantillas_msg WHERE id=?", (plantilla_map.get(tipo, tipo),))
    tpl = row_to_dict(c.fetchone())
    conn.close()
    if not tpl:
        return jsonify({'error': 'Plantilla no encontrada'}), 404

    destinatarios = destinatarios_comunicacion(tipo)
    template = tpl['mensaje_wsp'] if canal == 'whatsapp' else tpl['mensaje_sms']

    previews = []
    for dest in destinatarios[:10]:
        previews.append({
            'cliente': dest['nombre'],
            'celular': dest['celular'],
            'mensaje': fill_plantilla(template, dest)
        })

    return jsonify({
        'plantilla': tpl['nombre'],
        'canal': canal,
        'total_destinatarios': len(destinatarios),
        'previews': previews
    })

@app.route('/api/comunicacion/enviar', methods=['POST'])
@requiere_auth(roles=['admin','cobrador'])
def enviar_mensajes():
    d = request.get_json() or {}
    tipo = d.get('tipo')
    canal = d.get('canal', 'whatsapp')
    if not tipo:
        return jsonify({'error': 'tipo requerido'}), 400

    conn = get_db()
    c = conn.cursor()
    plantilla_map = {'prevencimiento':'prevencimiento','vencimiento':'vencimiento','postvencimiento':'postvencimiento'}
    c.execute("SELECT * FROM plantillas_msg WHERE id=?", (plantilla_map.get(tipo),))
    tpl = row_to_dict(c.fetchone())
    if not tpl:
        conn.close()
        return jsonify({'error': 'Plantilla no encontrada'}), 404

    destinatarios = destinatarios_comunicacion(tipo)
    template_wsp = tpl['mensaje_wsp']
    template_sms = tpl['mensaje_sms']
    enviados = 0
    canales = [canal] if canal != 'ambos' else ['whatsapp', 'sms']

    for dest in destinatarios:
        for ch in canales:
            template = template_wsp if ch == 'whatsapp' else template_sms
            mensaje_final = fill_plantilla(template, dest)
            # En producción: aquí se llama a la API de WhatsApp o Twilio
            # Por ahora se registra como "enviado" en el historial
            c.execute("""INSERT INTO comunicaciones
                (tipo, canal, plantilla_id, cliente_id, contrato_id,
                mensaje, estado, enviado_en, creado_por)
                VALUES (?,?,?,?,?,?,?,datetime('now'),?)""", (
                tipo, ch, tpl['id'], dest['cliente_id'], None,
                mensaje_final, 'enviado', request.usuario['sub']
            ))
            enviados += 1

    conn.commit()
    conn.close()
    return jsonify({
        'mensaje': f'{len(destinatarios)} cliente(s) notificados por {canal}',
        'total_mensajes': enviados,
        'destinatarios': len(destinatarios)
    })

@app.route('/api/comunicacion/historial', methods=['GET'])
@requiere_auth()
def historial_comunicacion():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT co.*, cl.nombre as cliente_nombre, u.nombre as usuario_nombre
        FROM comunicaciones co
        LEFT JOIN clientes cl ON cl.id = co.cliente_id
        LEFT JOIN usuarios u ON u.id = co.creado_por
        ORDER BY co.creado_en DESC LIMIT 100""")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/comunicacion/automatizaciones', methods=['GET'])
@requiere_auth()
def get_automatizaciones():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM automatizaciones")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/comunicacion/automatizaciones/<id>', methods=['PATCH'])
@requiere_auth(roles=['admin'])
def toggle_automatizacion(id):
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    if 'activo' in d:
        c.execute("UPDATE automatizaciones SET activo=? WHERE id=?", (int(d['activo']), id))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Actualizado'})

# ─── USUARIOS ─────────────────────────────────────────────────────────────────
@app.route('/api/usuarios', methods=['GET'])
@requiere_auth(roles=['admin'])
def get_usuarios():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, nombre, email, rol, activo, creado_en FROM usuarios ORDER BY nombre")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/usuarios', methods=['POST'])
@requiere_auth(roles=['admin'])
def crear_usuario():
    d = request.get_json() or {}
    required = ['nombre', 'email', 'password', 'rol']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    roles_validos = ['admin','vendedor','cobrador','contador','lectura']
    if d['rol'] not in roles_validos:
        return jsonify({'error': f'Rol inválido. Opciones: {", ".join(roles_validos)}'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM usuarios WHERE email=?", (d['email'],))
    if c.fetchone():
        conn.close()
        return jsonify({'error': 'Ya existe un usuario con ese email'}), 400
    c.execute("""INSERT INTO usuarios (nombre, email, password_hash, rol)
        VALUES (?,?,?,?)""", (d['nombre'], d['email'], hash_password(d['password']), d['rol']))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': new_id, 'mensaje': 'Usuario creado'}), 201

# ─── INICIO ───────────────────────────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'version': '1.0.0',
        'sistema': 'LoteAdmin — Sistema Inmobiliario Paraguay',
        'endpoints': [
            'POST /api/auth/login',
            'GET  /api/dashboard',
            'GET/POST /api/proyectos',
            'GET/POST /api/lotes',
            'GET/POST /api/clientes',
            'GET/POST /api/contratos',
            'GET      /api/cuotas',
            'GET      /api/cuotas/vencidas',
            'GET      /api/cuotas/proximas',
            'GET/POST /api/pagos',
            'GET      /api/reportes/resumen',
            'GET      /api/reportes/mora',
            'GET/POST /api/comunicacion/plantillas',
            'GET      /api/comunicacion/destinatarios/<tipo>',
            'POST     /api/comunicacion/preview',
            'POST     /api/comunicacion/enviar',
            'GET      /api/comunicacion/historial',
            'GET/POST /api/usuarios',
        ]
    })

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('RAILWAY_ENVIRONMENT') is None
    print("═" * 55)
    print("  Paraguay Connections — Backend API")
    print("═" * 55)
    init_db()
    print(f"✓ Servidor en puerto {port}")
    print("═" * 55)
    app.run(debug=debug, port=port, host='0.0.0.0')


@app.route('/api/lotes/<int:id>', methods=['PUT'])
@requiere_auth(roles=['admin'])
def actualizar_lote(id):
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    campos = ['numero','manzana','superficie_m2','precio_contado','precio_financiado','estado','observaciones']
    sets = [f"{k}=?" for k in campos if k in d]
    vals = [d[k] for k in campos if k in d]
    if sets:
        c.execute(f"UPDATE lotes SET {','.join(sets)} WHERE id=?", vals + [id])
        conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Lote actualizado'})

@app.route('/api/lotes/<int:id>', methods=['DELETE'])
@requiere_auth(roles=['admin'])
def eliminar_lote(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT estado FROM lotes WHERE id=?", (id,))
    lote = c.fetchone()
    if not lote:
        conn.close()
        return jsonify({'error': 'Lote no encontrado'}), 404
    if lote['estado'] in ('vendido', 'en_mora'):
        conn.close()
        return jsonify({'error': 'No se puede eliminar: el lote tiene un contrato activo'}), 400
    c.execute("DELETE FROM lotes WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Lote eliminado'})

@app.route('/api/clientes/<int:id>', methods=['DELETE'])
@requiere_auth(roles=['admin'])
def eliminar_cliente(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM contratos WHERE cliente_id=? AND estado NOT IN ('rescindido','cancelado')", (id,))
    if c.fetchone()['n'] > 0:
        conn.close()
        return jsonify({'error': 'No se puede eliminar: el cliente tiene contratos activos'}), 400
    c.execute("DELETE FROM clientes WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Cliente eliminado'})

# ─── CAJA Y DISTRIBUCIÓN ──────────────────────────────────────────────────────

@app.route('/api/caja/config', methods=['GET'])
@requiere_auth()
def get_caja_config():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM caja_config ORDER BY id")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/caja/config', methods=['POST'])
@requiere_auth(roles=['admin'])
def save_caja_config():
    d = request.get_json() or {}
    conn = get_db()
    c = conn.cursor()
    # d['conceptos'] = [{'nombre':..,'porcentaje':..,'tipo':..}, ...]
    c.execute("DELETE FROM caja_config")
    for con in d.get('conceptos', []):
        c.execute("INSERT INTO caja_config (nombre, porcentaje, tipo) VALUES (?,?,?)",
                  (con['nombre'], con['porcentaje'], con.get('tipo','egreso')))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Configuración guardada'})

@app.route('/api/caja/resumen', methods=['GET'])
@requiere_auth()
def get_caja_resumen():
    conn = get_db()
    c = conn.cursor()

    # Total cobrado
    c.execute("SELECT COALESCE(SUM(monto),0) as total FROM pagos")
    total_cobrado = c.fetchone()['total']

    # Egresos registrados
    c.execute("SELECT COALESCE(SUM(monto),0) as total FROM caja_egresos")
    total_egresos = c.fetchone()['total']

    # Caja disponible
    caja_disponible = total_cobrado - total_egresos

    # Configuración de distribución
    c.execute("SELECT * FROM caja_config")
    configs = rows_to_list(c.fetchall())

    # Distribución calculada sobre total cobrado
    distribucion = []
    for cfg in configs:
        monto = round(total_cobrado * cfg['porcentaje'] / 100)
        distribucion.append({
            'concepto': cfg['nombre'],
            'porcentaje': cfg['porcentaje'],
            'tipo': cfg['tipo'],
            'monto_calculado': monto
        })

    # Total pendiente por cobrar (cuotas no pagadas)
    c.execute("""SELECT COALESCE(SUM(total_a_pagar - COALESCE(monto_pagado,0)),0) as total
        FROM cuotas WHERE estado IN ('pendiente','vencida','parcial')""")
    total_pendiente = c.fetchone()['total']

    # Mora acumulada
    c.execute("SELECT COALESCE(SUM(mora+gastos_mora),0) as total FROM cuotas WHERE estado='vencida'")
    mora = c.fetchone()['total']

    # Egresos detallados recientes
    c.execute("""SELECT e.*, u.nombre as usuario_nombre
        FROM caja_egresos e LEFT JOIN usuarios u ON u.id=e.creado_por
        ORDER BY e.fecha DESC LIMIT 20""")
    egresos = rows_to_list(c.fetchall())

    # Cobros por mes (últimos 12)
    c.execute("""SELECT strftime('%Y-%m', fecha_pago) as mes,
        COALESCE(SUM(monto),0) as total, COUNT(*) as cantidad
        FROM pagos GROUP BY mes ORDER BY mes DESC LIMIT 12""")
    cobros_mes = rows_to_list(c.fetchall())

    conn.close()
    return jsonify({
        'total_cobrado': round(total_cobrado),
        'total_egresos': round(total_egresos),
        'caja_disponible': round(caja_disponible),
        'total_pendiente': round(total_pendiente),
        'mora_acumulada': round(mora),
        'distribucion': distribucion,
        'egresos_recientes': egresos,
        'cobros_por_mes': cobros_mes,
    })

@app.route('/api/caja/egresos', methods=['GET'])
@requiere_auth()
def get_egresos():
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT e.*, u.nombre as usuario_nombre
        FROM caja_egresos e LEFT JOIN usuarios u ON u.id=e.creado_por
        ORDER BY e.fecha DESC""")
    result = rows_to_list(c.fetchall())
    conn.close()
    return jsonify(result)

@app.route('/api/caja/egresos', methods=['POST'])
@requiere_auth(roles=['admin','contador'])
def crear_egreso():
    d = request.get_json() or {}
    required = ['concepto', 'monto', 'fecha']
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({'error': f'Campos requeridos: {", ".join(missing)}'}), 400
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO caja_egresos (concepto, monto, fecha, descripcion, creado_por)
        VALUES (?,?,?,?,?)""",
        (d['concepto'], float(d['monto']), d['fecha'],
         d.get('descripcion'), request.usuario['sub']))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'id': new_id, 'mensaje': 'Egreso registrado'}), 201

@app.route('/api/caja/egresos/<int:id>', methods=['DELETE'])
@requiere_auth(roles=['admin'])
def eliminar_egreso(id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM caja_egresos WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return jsonify({'mensaje': 'Egreso eliminado'})

# ─── ESTADO DE CUENTA CLIENTE ─────────────────────────────────────────────────

@app.route('/api/clientes/<int:id>/estado-cuenta', methods=['GET'])
@requiere_auth()
def estado_cuenta_cliente(id):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM clientes WHERE id=?", (id,))
    cliente = row_to_dict(c.fetchone())
    if not cliente:
        conn.close()
        return jsonify({'error': 'Cliente no encontrado'}), 404

    c.execute("""SELECT co.*, p.nombre as proyecto_nombre,
        l.numero as lote_numero, l.manzana
        FROM contratos co
        JOIN proyectos p ON p.id=co.proyecto_id
        JOIN lotes l ON l.id=co.lote_id
        WHERE co.cliente_id=?""", (id,))
    contratos = rows_to_list(c.fetchall())

    resultado = []
    for cont in contratos:
        c.execute("""SELECT cu.*, pa.monto as monto_pagado_reg, pa.fecha_pago as fecha_pago_reg,
            pa.medio
            FROM cuotas cu
            LEFT JOIN pagos pa ON pa.cuota_id=cu.id
            WHERE cu.contrato_id=? ORDER BY cu.numero""", (cont['id'],))
        cuotas = rows_to_list(c.fetchall())

        pagadas = [q for q in cuotas if q['estado']=='pagada']
        vencidas = [q for q in cuotas if q['estado']=='vencida']
        pendientes = [q for q in cuotas if q['estado']=='pendiente']

        total_pagado = sum(q['monto_pagado'] or 0 for q in cuotas)
        total_mora = sum((q['mora'] or 0)+(q['gastos_mora'] or 0) for q in vencidas)
        total_deuda = sum(q['total_a_pagar'] or 0 for q in vencidas)
        proxima = pendientes[0] if pendientes else None

        resultado.append({
            'contrato': cont,
            'cuotas': cuotas,
            'resumen': {
                'total_cuotas': len(cuotas),
                'cuotas_pagadas': len(pagadas),
                'cuotas_vencidas': len(vencidas),
                'cuotas_pendientes': len(pendientes),
                'total_pagado': round(total_pagado),
                'total_mora': round(total_mora),
                'total_deuda_vencida': round(total_deuda),
                'proxima_cuota': proxima,
            }
        })

    conn.close()
    return jsonify({'cliente': cliente, 'contratos': resultado})

