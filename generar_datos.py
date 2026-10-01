"""Genera data.json para la herramienta de pedidos de 611 Logistica.

Lee la replica BI de Gescom (Postgres) y vuelca:
  - venta neta diaria por articulo (FAC - NCR, en unidades), desde el inicio del historial
  - stock actual por articulo y deposito
  - maestro de articulos (proveedor, unidad de compra / factor de bulto)
  - ultima compra recibida por articulo

El calculo del pedido (promedio, cobertura, sugerido) se hace en el navegador,
asi la ventana del promedio y los dias a reponer se cambian en vivo.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2

TZ_AR = timezone(timedelta(hours=-3))

# Codigos que no son mercaderia: adelantos, notas de credito, articulos varios (proveedor "611")
# las cajas de carton recuperadas y el material POP / exhibidores (codigos 1500...).
EXCLUIR_PROV = ("611",)


def conectar():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "bi.data.gescom.online"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "gcwbi"),
        user=os.environ.get("PGUSER", "gcw-611logistica"),
        password=os.environ.get("PGPASSWORD") or None,  # sin variable, usa pgpass.conf
        connect_timeout=30,
    )


def leer_paletizado():
    """paletizado.csv (codigo;descripcion;bultos_por_camada;bultos_por_pallet), editable a mano."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paletizado.csv")
    pal = {}
    if not os.path.exists(ruta):
        return pal
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            cod = (r.get("codigo") or "").strip()
            cam = (r.get("bultos_por_camada") or "").strip()
            pall = (r.get("bultos_por_pallet") or "").strip()
            cam = int(float(cam.replace(",", "."))) if cam else 0
            pall = int(float(pall.replace(",", "."))) if pall else 0
            if cod and (cam > 0 or pall > 0):
                pal[cod] = (cam, pall)
    return pal


def main():
    hoy = datetime.now(TZ_AR).date()
    cur = conectar().cursor()

    # Maestro de articulos vendibles (con proveedor real)
    cur.execute(
        """
        SELECT i.id, i.codigo, i.descripcion, i.prov_codigo, trim(i.prov_razonsocial),
               i.marca_desc, i.rubro_desc, iu.unidad_nombre, iu.factor, i.activo
        FROM item i
        LEFT JOIN item_unidad iu ON iu.id = i.unidad_compra_id
        WHERE i.prov_codigo IS NOT NULL AND i.prov_codigo <> ALL(%s)
          AND i.descripcion NOT ILIKE '%%carton%%'
          AND i.codigo NOT LIKE '1500%%'
        """,
        (list(EXCLUIR_PROV),),
    )
    items = {}
    proveedores = {}
    for iid, cod, desc, pcod, prs, marca, rubro, un, factor, activo in cur.fetchall():
        items[iid] = {
            "c": cod, "d": (desc or "").replace("\xa0", " ").strip(), "p": pcod,
            "m": marca or "", "r": rubro or "",
            "u": un or "Unidad", "f": int(factor or 1), "a": int(activo or 0),
        }
        proveedores[pcod] = prs or pcod

    # Venta neta diaria por articulo: facturas suman, notas de credito restan.
    # Remitos (envases / exhibidores) y notas de debito (ajustes de precio) no cuentan.
    cur.execute(
        """
        SELECT v.fecha_comprobante::date, vi.item_id,
               sum(CASE WHEN v.tipo_comprobante_codigo LIKE 'FAC%%' THEN vi.cantidad * vi.unidad_factor
                        ELSE -vi.cantidad * vi.unidad_factor END)
        FROM venta v
        JOIN venta_item vi ON vi.venta_id = v.id
        WHERE (v.tipo_comprobante_codigo LIKE 'FAC%%' OR v.tipo_comprobante_codigo LIKE 'NCR%%')
          AND v.fecha_comprobante < %s
        GROUP BY 1, 2
        """,
        (hoy,),
    )
    filas = [r for r in cur.fetchall() if r[1] in items]
    # El historial de detalle real empieza cuando el volumen diario es consistente;
    # los pocos dias sueltos anteriores (sync parcial) se descartan.
    por_dia = defaultdict(int)
    for f, _, _ in filas:
        por_dia[f] += 1
    dias_buenos = sorted(d for d, n in por_dia.items() if n >= 50)
    desde = dias_buenos[0] if dias_buenos else hoy
    hasta = hoy - timedelta(days=1)

    ventas = defaultdict(dict)
    vendidos = set()
    for f, iid, q in filas:
        if f < desde:
            continue
        idx = (f - desde).days
        ventas[items[iid]["c"]][idx] = round(float(q), 2)
        if q > 0:
            vendidos.add(iid)

    # Stock actual por deposito
    cur.execute(
        "SELECT item_codigo, deposito_codigo, deposito_desc, cantidad FROM stock WHERE cantidad <> 0"
    )
    depositos = {}
    stock = defaultdict(dict)
    for cod, dcod, ddesc, q in cur.fetchall():
        depositos[dcod] = ddesc
        stock[cod][dcod] = float(q)

    # Ultima compra recibida por articulo
    cur.execute(
        "SELECT item_id, max(fecha_comprobante)::date FROM compra_item GROUP BY 1"
    )
    ult_compra = {iid: f.isoformat() for iid, f in cur.fetchall() if f}

    paletizado = leer_paletizado()
    codigos_con_stock = set(stock)
    salida_items = []
    for iid, it in items.items():
        if iid not in vendidos and it["c"] not in codigos_con_stock:
            continue
        if iid not in vendidos and not it["a"]:
            continue
        it = dict(it)
        it["s"] = stock.get(it["c"], {})
        it["uc"] = ult_compra.get(iid)
        it["v"] = ventas.get(it["c"], {})
        if it["c"] in paletizado:
            it["cam"], it["pal"] = paletizado[it["c"]]
        salida_items.append(it)
    salida_items.sort(key=lambda x: (x["p"], x["d"]))

    data = {
        "generado": datetime.now(TZ_AR).strftime("%Y-%m-%d %H:%M"),
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "proveedores": proveedores,
        "depositos": depositos,
        "items": salida_items,
    }
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json"), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(salida_items)} articulos, historial {desde} -> {hasta}")


if __name__ == "__main__":
    main()
