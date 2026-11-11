"""Review interface. Run with:  python -m cierre_cxc ui --datos <file.xlsx>

The UI never computes amounts: every change (approve, reject, reassign a receipt) is stored as a decision,
the engine reruns, and the audit log records who did what.
"""
from __future__ import annotations
import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from cierre_cxc.aprendizaje import Reglas                                    # noqa: E402
from cierre_cxc.asistente_llm import redactar_cierre                         # noqa: E402
from cierre_cxc.auditoria import Bitacora, sha256_archivo                    # noqa: E402
from cierre_cxc.cargar import SchemaError                                    # noqa: E402
from cierre_cxc.cli import claves_revisables                                 # noqa: E402
from cierre_cxc.exportar import a_excel, conteo_niveles, todas_las_tablas    # noqa: E402
from cierre_cxc.pipeline import ejecutar                                     # noqa: E402
from cierre_cxc.revision import (Revision, RevisionError, clave_match,  # noqa: E402
                                 claves_excepciones, claves_polizas)


def _args():
    p = argparse.ArgumentParser()
    p.add_argument("--datos", default="")
    p.add_argument("--salida", default="salida")
    a, _ = p.parse_known_args(sys.argv[1:])
    return a


def _df(filas):
    df = pd.DataFrame(filas)
    for c in df.columns:
        if df[c].map(lambda v: isinstance(v, Decimal)).any():
            df[c] = df[c].map(lambda v: float(v) if isinstance(v, Decimal) else v)
    return df


args = _args()
st.set_page_config(page_title="AR close review", layout="wide")
st.title("AR close: review and approval")

with st.sidebar:
    usuario = st.text_input("Reviewer name (required to approve)", value=st.session_state.get("usuario", "")).strip()
    st.session_state["usuario"] = usuario
    salida = Path(st.text_input("Output folder", value=args.salida))
    subido = st.file_uploader("Dataset (.xlsx)", type=["xlsx"])
    ruta_txt = st.text_input("...or path to the dataset", value=args.datos)
    usar_llm = st.checkbox("Let a language model suggest customers for unidentified receipts", value=False,
                           help="Needs ANTHROPIC_API_KEY. The model only suggests a customer; amounts are always computed in code.")

ruta = None
if subido is not None:
    destino = salida / "entrada" / subido.name
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(subido.getvalue())
    ruta = destino
elif ruta_txt:
    ruta = Path(ruta_txt)
if not ruta:
    st.info("Upload a dataset or give its path in the sidebar.")
    st.stop()

try:
    bit = Bitacora(salida / "bitacora_auditoria.jsonl", usuario or None)
except OSError as e:
    st.error(f"Cannot use the output folder '{salida}': {e.strerror or e}. Give a folder path.")
    st.stop()
try:
    rev = Revision(salida / "revision.json", bit, huella=sha256_archivo(ruta) if Path(ruta).is_file() else None)
except RevisionError as e:
    st.error(str(e))
    st.stop()
if rev.discrepancias:
    st.warning("Notice: revision.json does not match the audit log (an older copy put back, or edited by hand?): "
         + "; ".join(rev.discrepancias) + ". Every approval counts as pending until it matches: redo or "
         "remove the override or rejection so the log records it.")
if rev.lineas_danadas:
    st.warning(f"Notice: the audit log has {rev.lineas_danadas} damaged line(s). Approvals given before the last "
         "damaged line count as pending; the damaged lines are listed as damaged_line in Audit_Log.")
if rev.sin_respaldo:
    st.warning(f"{len(rev.sin_respaldo)} approval(s) in revision.json have no matching decision in the audit log "
               "(edited by hand?). They count as pending.")
if rev.reiniciada:
    st.warning(f"This file is not the one reviewed before (SHA-256 {rev.reiniciada[:12]}...). Earlier approvals and "
               f"overrides do not apply to it; they were kept in {rev.apartado}.")
sugeridor = None
if usar_llm:
    from cierre_cxc.asistente_llm import crear_sugeridor
    sugeridor = crear_sugeridor(bit)
try:
    reglas = Reglas(salida / "reglas_aprendidas.json", bit)
    s = ejecutar(ruta, decisiones=rev.como_decisiones(), bitacora=bit, sugeridor=sugeridor, reglas=reglas)
    if s.reglas_retiradas:          # the receipts those rules placed move: earlier approvals no longer apply
        rev.reabrir("learned rule retired " + ", ".join(r["id"] for r in s.reglas_retiradas))
        s = ejecutar(ruta, decisiones=rev.como_decisiones(), bitacora=bit, sugeridor=sugeridor,
                     reglas=Reglas(salida / "reglas_aprendidas.json", bit))
except (SchemaError, RevisionError) as e:
    st.error(f"Input error: {e}")
    st.stop()
for a in s.res.avisos:
    if a.startswith(("Learned rule", "Learned rules are NOT used")):
        st.warning(a)
for r in s.reglas_nuevas:
    st.success(f"Learned rule {r['id']}: a deposit whose description has '{' '.join(r['tokens'])}' is from {r['cliente']}.")

claves = claves_revisables(s)
pend = rev.pendientes(claves)
# Keep full amounts visible: Streamlit cuts metric values with an ellipsis when they do not fit the column.
st.markdown("<style>[data-testid='stMetricValue'] {font-size: 1.7rem;}</style>", unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Closing AR (MXN)", f"{s.aging['total']:,.2f}")
c2.metric("IVA cobrado (MXN)", f"{s.iva['cobrado']:,.2f}")
c3.metric("Exceptions", len(s.excepciones), f"{sum(e.severidad == 'alta' for e in s.excepciones)} high")
c4.metric("Items pending review", len(pend))
if s.controles_ok:
    st.success("All internal checks pass (entries balance, roll-forward ties to aging, IVA ties, every receipt accounted for).")
else:
    st.error("Some internal checks FAIL: " + "; ".join(n for n, ok, _ in s.controles if not ok))
for a in s.res.avisos:
    st.warning(a)


def decision(clave, etiqueta):
    estado = rev.estado(clave)
    cols = st.columns([4, 1.5, 2.5, 1.6, 1.6])   # wide enough for the full "Approve" / "Reject" labels
    cols[0].markdown(etiqueta)
    cols[1].markdown(f"**{estado}**")
    com = cols[2].text_input("Comment", key=f"c_{clave}", label_visibility="collapsed", placeholder="comment")
    if cols[3].button("Approve", key=f"a_{clave}", disabled=not usuario):
        rev.decidir(clave, "aprobado", usuario, com); st.rerun()
    if cols[4].button("Reject", key=f"r_{clave}", disabled=not usuario):
        rev.decidir(clave, "rechazado", usuario, com); st.rerun()


tabs = st.tabs(["A1 Cash application", "A2 Exceptions", "A3 Journal entries", "A4 Aging", "A5 IVA", "Checks & audit",
                "AI draft"])
t = todas_las_tablas(s, rev)

with tabs[0]:
    st.subheader("Bank movements")
    st.dataframe(_df(t["A1_Bank_Movements"]), width="stretch", hide_index=True)
    st.caption("Receipts: " + " | ".join(f"{n}: {k}" for n, k in conteo_niveles(s).items()))
    a_confirmar = [u for u in s.res.efectivo_no_identificado if u.get("nivel") == 2]
    if a_confirmar:
        st.subheader("Receipts to confirm (only a clue: nothing is applied until you accept a candidate)")
    for u in a_confirmar:
        st.markdown(f"`{u['mov_id']}` {u['fecha']} {u['monto']:,.2f} {u['moneda']} - {u['descripcion']}")
        for i, c in enumerate(u["candidatos"], start=1):
            cols = st.columns([6, 2])
            cols[0].markdown(f"[{i}] **{c['folio']}** {c['cliente']}, balance {c['saldo']:,.2f} {c['moneda']}: {c['evidencia']}")
            if cols[1].button(f"Accept {c['folio']}", key=f"acc_{u['mov_id']}_{c['folio']}", disabled=not usuario):
                rev.anular_conciliacion(u["mov_id"], "asignar", usuario, [c["folio"]],
                                        f"accepted proposed candidate {i}: {c['evidencia']}")
                st.rerun()
    st.subheader("Review each proposed match")
    for m in s.res.movimientos:
        aps = [a for a in s.res.aplicaciones if a.mov_id == m.id]
        unid = any(u["mov_id"] == m.id for u in s.res.efectivo_no_identificado)
        if not aps and not unid:
            continue
        destino = ", ".join(f"{a.folio} ({a.tipo}, {a.confianza})" for a in aps) or "UNIDENTIFIED"
        decision(clave_match(m.id), f"`{m.id}` {m.fecha} {m.abono:,.2f} {m.moneda} - {m.descripcion} -> **{destino}**")
    st.subheader("Override a match")
    with st.form("override"):
        mov = st.selectbox("Movement", [m.id for m in s.res.movimientos if m.abono > 0])
        folios = st.multiselect("Apply to invoices (in this order)", sorted(s.res.partidas))
        accion = st.radio("Or", ["assign to the invoices above", "not AR", "leave unidentified"], horizontal=True)
        com = st.text_input("Reason (required)")
        if st.form_submit_button("Save override and recompute", disabled=not usuario):
            if not com.strip():
                st.error("Write the reason for the override.")
            else:
                codigo = {"assign to the invoices above": "asignar", "not AR": "no_cxc", "leave unidentified": "no_identificado"}[accion]
                try:
                    rev.anular_conciliacion(mov, codigo, usuario, folios if codigo == "asignar" else [], com)
                except RevisionError as e:
                    st.error(str(e))
                else:
                    st.rerun()
    if rev.datos["conciliacion"]:
        st.caption("Active overrides: " + ", ".join(f"{k} -> {v['accion']} {v['folios']} ({v['usuario']})"
                                                   for k, v in rev.datos["conciliacion"].items()))
        quitar = st.selectbox("Remove override", [""] + list(rev.datos["conciliacion"]))
        if quitar and st.button("Remove", disabled=not usuario):
            rev.quitar_anulacion(quitar, usuario); st.rerun()
    if s.reglas:
        st.subheader("Learned rules (from reviewers' decisions)")
        st.dataframe(pd.DataFrame([{"id": r["id"], "words": " ".join(r["tokens"]), "customer": r["cliente"],
                                    "taught by": r["usuario"], "from": r["movimiento"]} for r in s.reglas]),
                     width="stretch", hide_index=True)
        regla_q = st.selectbox("Stop using a rule", [""] + [r["id"] for r in s.reglas])
        if regla_q and st.button("Stop using it", disabled=not usuario):
            reglas.quitar(regla_q, usuario, "removed in UI")
            rev.reabrir("learned rule removed " + regla_q)
            st.rerun()

with tabs[1]:
    st.dataframe(_df(t["A2_Exceptions"]), width="stretch", hide_index=True)
    claves_exc = dict(zip(map(id, s.excepciones), claves_excepciones(s.excepciones)))
    for e in sorted(s.excepciones, key=lambda x: {"alta": 0, "media": 1, "baja": 2}[x.severidad]):
        decision(claves_exc[id(e)], f"`{e.id}` **{e.severidad}** {e.categoria} - {e.referencia}: {e.que}")

with tabs[2]:
    st.dataframe(_df(t["A3_Journal_Entries"]), width="stretch", hide_index=True)
    for p, clave_p in zip(s.polizas, claves_polizas(s.polizas)):
        etiqueta = f"`{p.id}` {p.tipo} - {p.concepto}" + (" *(conditional)*" if p.estado == "condicionada" else "")
        decision(clave_p, etiqueta)
    cond = [k["folio"] for k in s.res.correcciones_tc if k["aplicada"]]
    if cond:
        f = st.selectbox("Reject a conditional rate correction", [""] + cond)
        if f and st.button("Reject correction and recompute", disabled=not usuario):
            rev.rechazar_correccion(f, usuario, "rejected in UI"); st.rerun()

with tabs[3]:
    st.dataframe(_df(t["A4_Aging"]), width="stretch", hide_index=True)
    st.subheader("Roll-forward")
    st.dataframe(_df(t["A4_Roll_Forward"]), width="stretch", hide_index=True)

with tabs[4]:
    st.dataframe(_df(t["A5_IVA_Collected"]), width="stretch", hide_index=True)
    st.dataframe(_df(t["A5_IVA_Roll_Forward"]), width="stretch", hide_index=True)

with tabs[5]:
    st.dataframe(pd.DataFrame([{"check": n, "result": "PASS" if ok else "FAIL", "detail": d} for n, ok, d in s.controles]),
                 width="stretch", hide_index=True)
    st.subheader("Audit log")
    st.dataframe(pd.DataFrame(bit.leer()).astype(str), width="stretch", hide_index=True)

with tabs[6]:
    st.caption("Optional. One call to the language model with the figures the tool already computed. It drafts the "
               "summary for the Controller, each high exception in plain words and one email per owner. Every number "
               "and folio in the draft is checked against the close; if one is not there, the template is used. It is "
               "a draft: nothing is sent or posted, and a reviewer approves it.")
    if st.button("Draft with AI", key="ia_borrador"):
        with st.spinner("Drafting..."):
            st.session_state["borrador_ia"] = redactar_cierre(s, bit)
    b = st.session_state.get("borrador_ia")
    if b:
        if b["origen"] == "ia":
            st.success(f"AI draft verified: {b['verificados']['numeros']} numbers and {b['verificados']['folios']} "
                       f"folios match the close (model {b['modelo']}). Check the wording before approving.")
        else:
            st.warning(f"Template draft (no AI text was used): {b['motivo']}")
        st.subheader("Summary for the Controller")
        st.markdown(b["resumen"])
        if b["excepciones"]:
            st.subheader("High exceptions in plain words")
            st.dataframe(pd.DataFrame([{"referencia": x.get("referencia", ""), "familia": x.get("familia", ""),
                                        "explicacion": x.get("explicacion", "")} for x in b["excepciones"]]),
                         width="stretch", hide_index=True)
        if b["correos"]:
            st.subheader("Draft emails (not sent)")
            for i, c in enumerate(b["correos"]):
                with st.expander(f"To: {c['para']} - {c['asunto']}"):
                    st.text_area("Body", c["cuerpo"], key=f"correo_{i}", height=220)
        if st.button("Approve draft as " + (usuario or "..."), key="ia_aprobar", disabled=not usuario):
            bit.registrar("draft_approved", origen=b["origen"], modelo=b.get("modelo"),
                          numeros=b["verificados"]["numeros"], folios=b["verificados"]["folios"])
            st.success("Draft approved and recorded in the audit log.")

st.divider()
if usuario and st.button("Approve every pending item as " + usuario):
    for c in pend:
        rev.decidir(c, "aprobado", usuario, "bulk approval in UI")
    st.rerun()
borrador = bool(rev.pendientes(claves))
nombre = f"{s.res.cierre:%Y-%m}_cierre_cxc_{'BORRADOR' if borrador else 'APROBADO'}.xlsx"
if st.button("Build Excel"):
    try:
        xlsx = a_excel(s, salida / nombre, rev, bit, borrador=borrador)
    except PermissionError:
        st.error(f"Cannot write {nombre}: it is open in another program (probably Excel). Close it and try again.")
        st.stop()
    bit.registrar("export", archivo=xlsx.name, sha256=sha256_archivo(xlsx), pendientes=len(rev.pendientes(claves)),
                  borrador=borrador)
    st.download_button("Download " + nombre, xlsx.read_bytes(), file_name=nombre)
