from datetime import datetime, date, timedelta
from db import get_db
import math

def calcular_cuota_revaluada(cuota_base, numero_cuota, revaluacion_pct=5.0):
    """Aplica revaluación anual desde la cuota 13 en adelante."""
    anio = math.floor((numero_cuota - 1) / 12)
    if anio == 0:
        return cuota_base
    factor = (1 + revaluacion_pct / 100) ** anio
    return round(cuota_base * factor)

def calcular_fecha_cuota(fecha_primer_venc_str, numero_cuota):
    """Calcula la fecha de vencimiento de cada cuota."""
    fecha = datetime.strptime(fecha_primer_venc_str, "%Y-%m-%d").date()
    mes = fecha.month + (numero_cuota - 1)
    anio = fecha.year + (mes - 1) // 12
    mes = ((mes - 1) % 12) + 1
    # Ajustar días inválidos (ej: 31 de febrero → último día del mes)
    ultimo_dia = [31,28+int(anio%4==0 and (anio%100!=0 or anio%400==0)),31,30,31,30,31,31,30,31,30,31][mes-1]
    dia = min(fecha.day, ultimo_dia)
    return date(anio, mes, dia)

def calcular_mora(monto_revaluado, fecha_venc_str, interes_mora_pct, gastos_mora_pct):
    """Calcula mora e intereses si la cuota está vencida."""
    hoy = date.today()
    fecha_venc = datetime.strptime(fecha_venc_str, "%Y-%m-%d").date()
    if hoy <= fecha_venc:
        return 0, 0, 0
    dias_atraso = (hoy - fecha_venc).days
    mora = round(monto_revaluado * interes_mora_pct / 100)
    gastos = round(monto_revaluado * gastos_mora_pct / 100)
    return mora, gastos, dias_atraso

def generar_plan_cuotas(contrato_id):
    """Genera el plan de pagos completo para un contrato."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM contratos WHERE id=?", (contrato_id,))
    contrato = c.fetchone()
    if not contrato:
        conn.close()
        return False

    # Borrar cuotas existentes
    c.execute("DELETE FROM cuotas WHERE contrato_id=?", (contrato_id,))

    for n in range(1, contrato['cantidad_cuotas'] + 1):
        fecha_venc = calcular_fecha_cuota(contrato['fecha_primer_venc'], n)
        monto_rev = calcular_cuota_revaluada(contrato['cuota_base'], n, contrato['revaluacion_pct'])
        mora, gastos, _ = calcular_mora(monto_rev, fecha_venc.isoformat(), contrato['interes_mora_pct'], contrato['gastos_mora_pct'])
        total = monto_rev + mora + gastos
        hoy = date.today()
        if fecha_venc < hoy:
            estado = 'vencida'
        else:
            estado = 'pendiente'
        c.execute("""
            INSERT INTO cuotas (contrato_id, numero, fecha_vencimiento,
                monto_base, monto_revaluado, mora, gastos_mora,
                total_a_pagar, estado, saldo_pendiente)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (contrato_id, n, fecha_venc.isoformat(),
              contrato['cuota_base'], monto_rev, mora, gastos,
              total, estado, total))

    conn.commit()
    conn.close()
    return True

def actualizar_mora_contrato(contrato_id):
    """Recalcula mora actualizada de todas las cuotas vencidas de un contrato."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM contratos WHERE id=?", (contrato_id,))
    contrato = c.fetchone()
    if not contrato:
        conn.close()
        return

    c.execute("SELECT * FROM cuotas WHERE contrato_id=? AND estado IN ('vencida','pendiente')", (contrato_id,))
    cuotas = c.fetchall()
    hoy = date.today()

    for cuota in cuotas:
        fecha_venc = datetime.strptime(cuota['fecha_vencimiento'], "%Y-%m-%d").date()
        if fecha_venc >= hoy:
            # Pendiente, no se recalcula mora
            continue
        mora, gastos, _ = calcular_mora(
            cuota['monto_revaluado'],
            cuota['fecha_vencimiento'],
            contrato['interes_mora_pct'],
            contrato['gastos_mora_pct']
        )
        total = cuota['monto_revaluado'] + mora + gastos
        saldo = max(0, total - cuota['monto_pagado'])
        c.execute("""UPDATE cuotas SET mora=?, gastos_mora=?, total_a_pagar=?,
            saldo_pendiente=?, estado='vencida' WHERE id=?""",
            (mora, gastos, total, saldo, cuota['id']))

    conn.commit()
    conn.close()

def estado_contrato(contrato_id):
    """Determina el estado actual del contrato según cuotas vencidas."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM cuotas WHERE contrato_id=? AND estado='vencida'", (contrato_id,))
    vencidas = c.fetchone()['n']
    c.execute("SELECT COUNT(*) as n, SUM(CASE WHEN estado='pagada' THEN 1 ELSE 0 END) as pagadas FROM cuotas WHERE contrato_id=?", (contrato_id,))
    row = c.fetchone()
    conn.close()
    total = row['n']
    pagadas = row['pagadas'] or 0
    if pagadas == total:
        return 'finalizado'
    if vencidas > 0:
        return 'en_mora'
    return 'vigente'

def resumen_contrato(contrato_id):
    """Resumen financiero de un contrato."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM cuotas WHERE contrato_id=?", (contrato_id,))
    cuotas = c.fetchall()
    conn.close()
    total_pagado = sum(q['monto_pagado'] for q in cuotas)
    total_mora = sum(q['mora'] + q['gastos_mora'] for q in cuotas if q['estado'] == 'vencida')
    total_pendiente = sum(q['saldo_pendiente'] or q['total_a_pagar'] for q in cuotas if q['estado'] != 'pagada')
    vencidas = [q for q in cuotas if q['estado'] == 'vencida']
    pagadas = [q for q in cuotas if q['estado'] == 'pagada']
    return {
        'total_cuotas': len(cuotas),
        'cuotas_pagadas': len(pagadas),
        'cuotas_vencidas': len(vencidas),
        'cuotas_pendientes': len([q for q in cuotas if q['estado'] == 'pendiente']),
        'total_pagado': round(total_pagado),
        'total_mora_acumulada': round(total_mora),
        'total_pendiente': round(total_pendiente),
    }

def destinatarios_comunicacion(tipo):
    """Retorna lista de clientes que deben recibir mensaje según el tipo."""
    conn = get_db()
    c = conn.cursor()
    hoy = date.today()
    resultado = []

    if tipo == 'prevencimiento':
        limite = (hoy + timedelta(days=2)).isoformat()
        c.execute("""
            SELECT cu.*, co.cliente_id, co.proyecto_id, co.lote_id,
                   co.interes_mora_pct, co.gastos_mora_pct
            FROM cuotas cu
            JOIN contratos co ON co.id = cu.contrato_id
            WHERE cu.estado = 'pendiente'
              AND cu.fecha_vencimiento <= ?
              AND cu.fecha_vencimiento >= ?
        """, (limite, hoy.isoformat()))

    elif tipo == 'vencimiento':
        c.execute("""
            SELECT cu.*, co.cliente_id, co.proyecto_id, co.lote_id,
                   co.interes_mora_pct, co.gastos_mora_pct
            FROM cuotas cu
            JOIN contratos co ON co.id = cu.contrato_id
            WHERE cu.estado = 'pendiente'
              AND cu.fecha_vencimiento = ?
        """, (hoy.isoformat(),))

    elif tipo == 'postvencimiento':
        desde = (hoy - timedelta(days=2)).isoformat()
        c.execute("""
            SELECT cu.*, co.cliente_id, co.proyecto_id, co.lote_id,
                   co.interes_mora_pct, co.gastos_mora_pct
            FROM cuotas cu
            JOIN contratos co ON co.id = cu.contrato_id
            WHERE cu.estado = 'vencida'
              AND cu.fecha_vencimiento >= ?
              AND cu.fecha_vencimiento < ?
        """, (desde, hoy.isoformat()))

    cuotas = c.fetchall()

    for cu in cuotas:
        c.execute("""
            SELECT cl.*, p.nombre as proyecto_nombre, p.id as pid,
                   l.numero as lote_numero, l.manzana
            FROM clientes cl
            JOIN proyectos p ON p.id=?
            JOIN lotes l ON l.id=?
            WHERE cl.id=?
        """, (cu['proyecto_id'], cu['lote_id'], cu['cliente_id']))
        info = c.fetchone()
        if info:
            resultado.append({
                'cliente_id': cu['cliente_id'],
                'nombre': info['nombre'],
                'celular': info['celular'],
                'proyecto': info['proyecto_nombre'],
                'lote': f"Mza {info['manzana']} - N°{info['lote_numero']}",
                'num_cuota': cu['numero'],
                'fecha_vencimiento': cu['fecha_vencimiento'],
                'monto_cuota': round(cu['monto_revaluado']),
                'mora_total': round(cu['mora'] + cu['gastos_mora']),
                'total_deuda': round(cu['total_a_pagar']),
                'cuota_id': cu['id'],
            })

    conn.close()
    return resultado

def fill_plantilla(template, datos, telefono_admin="+595 981 000 000"):
    """Reemplaza variables en plantilla con datos reales del cliente."""
    return (template
        .replace('{nombre_cliente}', datos.get('nombre',''))
        .replace('{num_cuota}', str(datos.get('num_cuota','')))
        .replace('{proyecto}', datos.get('proyecto',''))
        .replace('{lote}', datos.get('lote',''))
        .replace('{fecha_vencimiento}', datos.get('fecha_vencimiento',''))
        .replace('{monto_cuota}', f"{datos.get('monto_cuota',0):,}".replace(',','.'))
        .replace('{mora_total}', f"{datos.get('mora_total',0):,}".replace(',','.'))
        .replace('{total_deuda}', f"{datos.get('total_deuda',0):,}".replace(',','.'))
        .replace('{telefono_admin}', telefono_admin)
    )
