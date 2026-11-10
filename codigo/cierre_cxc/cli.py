"""Command line: run the close, review and approve, override a match, launch the review UI."""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

from .aprendizaje import Reglas
from .auditoria import Bitacora, sha256_archivo
from .cargar import SchemaError
from .config import Config
from .exportar import a_csv, a_excel, conteo_niveles
from .pipeline import ejecutar
from .revision import Revision, RevisionError, clave_match, claves_excepciones, claves_polizas


def claves_revisables(s) -> list:
    movs = sorted({a.mov_id for a in s.res.aplicaciones} | {u["mov_id"] for u in s.res.efectivo_no_identificado})
    return ([clave_match(m) for m in movs] + claves_excepciones(s.excepciones) + claves_polizas(s.polizas))


def _preparar(args):
    salida = Path(args.salida)
    bit = Bitacora(salida / "bitacora_auditoria.jsonl", args.usuario)
    datos = getattr(args, "datos", None)
    huella = sha256_archivo(datos) if datos and Path(datos).is_file() else None
    rev = Revision(salida / "revision.json", bit, huella=huella)
    if rev.discrepancias:
        print("Notice: revision.json does not match the audit log (an older copy put back, or edited by hand?): "
             + "; ".join(rev.discrepancias) + ". Every approval counts as pending until it matches: redo or "
             "remove the override or rejection so the log records it.")
    if rev.lineas_danadas:
        print(f"Notice: the audit log has {rev.lineas_danadas} damaged line(s). Approvals given before the last "
             "damaged line count as pending; the damaged lines are listed as damaged_line in Audit_Log.")
    if rev.sin_respaldo:
        print(f"Notice: {len(rev.sin_respaldo)} approval(s) in revision.json have no matching decision in the audit log "
              "(edited by hand?). They count as pending.")
    if rev.reiniciada:
        print(f"Notice: the input file is not the one reviewed before (SHA-256 {rev.reiniciada[:12]}...). Earlier "
              f"approvals and overrides do not apply to it; they were kept in {rev.apartado} and the review starts again.")
    return salida, bit, rev


def _correr(args, bit, rev, cfg=None):
    cfg = cfg or Config()
    if getattr(args, "cierre", None):
        cfg.cierre = date.fromisoformat(args.cierre)
    sugeridor = None
    if getattr(args, "llm", False):
        from .asistente_llm import crear_sugeridor
        sugeridor = crear_sugeridor(bit)
    s = ejecutar(args.datos, cfg, rev.como_decisiones(), bit, sugeridor=sugeridor,
                 reglas=Reglas(Path(args.salida) / "reglas_aprendidas.json", bit))
    for a in s.res.avisos:
        if a.startswith(("Learned rule", "Learned rules are NOT used")):
            print("Notice: " + a)
    if s.reglas_retiradas:
        rev.reabrir("learned rule retired " + ", ".join(r["id"] for r in s.reglas_retiradas))
        s = ejecutar(args.datos, cfg, rev.como_decisiones(), bit, sugeridor=sugeridor,
                     reglas=Reglas(Path(args.salida) / "reglas_aprendidas.json", bit))
    for r in s.reglas_nuevas:
        print(f"Learned rule {r['id']}: a deposit whose description has '{' '.join(r['tokens'])}' is from {r['cliente']} "
              f"(taught by {r['usuario'] or 'a reviewer'} on {r['movimiento']}).")
    return s


def cmd_run(args) -> int:
    salida, bit, rev = _preparar(args)
    s = _correr(args, bit, rev)
    pend = rev.pendientes(claves_revisables(s))
    borrador = bool(pend)
    nombre = f"{s.res.cierre:%Y-%m}_cierre_cxc_{'BORRADOR' if borrador else 'APROBADO'}"
    xlsx = a_excel(s, salida / f"{nombre}.xlsx", rev, bit, borrador=borrador)
    csvs = a_csv(s, salida / "csv", rev)
    # the hash of the exported workbook lets anyone prove later that it was not edited by hand
    bit.registrar("export", archivo=xlsx.name, sha256=sha256_archivo(xlsx), pendientes=len(pend), borrador=borrador,
                  csv=len(csvs))
    origen = "--cierre" if getattr(args, "cierre", None) else "the month with most bank rows; use --cierre to change it"
    print(f"Period: {s.res.inicio} to {s.res.cierre} ({origen}). Input: {s.archivo} (SHA-256 {s.huella[:12]}...)")
    print(f"Close {s.res.cierre}: {len(s.res.aplicaciones)} applications, {len(s.excepciones)} exceptions, {len(s.polizas)} entries.")
    print("Checks: " + ("ALL PASS" if s.controles_ok else "FAILED"))
    for n, ok, d in s.controles:
        if not ok:
            print(f"  FAIL {n}: {d}")
    print(f"Closing AR {s.aging['total']:,.2f} MXN | IVA cobrado {s.iva['cobrado']:,.2f} | IVA por trasladar {s.iva['cierre']:,.2f}")
    print("Receipts: " + " | ".join(f"{n}: {k}" for n, k in conteo_niveles(s).items()))
    for u in s.res.efectivo_no_identificado:
        if u.get("nivel") == 2:
            print(f"  To confirm {u['mov_id']}: " + "; ".join(f"[{i}] {c['folio']} {c['cliente']} ({c['evidencia']})"
                                                         for i, c in enumerate(u["candidatos"], start=1))
                  + f"  -> python -m cierre_cxc accept {u['mov_id']} --candidato 1 ...")
    print(f"Review status: {'DRAFT, ' + str(len(pend)) + ' items pending' if borrador else 'APPROVED'}")
    print(f"Excel: {xlsx}")
    return 0 if s.controles_ok else 2


def cmd_approve(args) -> int:
    if not args.usuario:
        print("A reviewer name is required (--usuario).", file=sys.stderr)
        return 1
    salida, bit, rev = _preparar(args)
    s = _correr(args, bit, rev)
    if not (args.items or "").strip():
        print("--items is empty. Use 'all', a prefix (match:, exc:, pol:) or an exact item key.", file=sys.stderr)
        return 1
    todas = claves_revisables(s)
    objetivo = todas if args.items == "all" else [c for c in todas if c.startswith(args.items) or c == args.items]
    if not objetivo:
        print(f"No item matches '{args.items}'.", file=sys.stderr)
        return 1
    for c in objetivo:
        rev.decidir(c, args.estado, args.usuario, args.comentario or "")
    print(f"{len(objetivo)} item(s) marked {args.estado} by {args.usuario}.")
    return 0


def cmd_override(args) -> int:
    salida, bit, rev = _preparar(args)
    folios = [x.strip() for x in (args.asignar or "").split(",") if x.strip()]
    if args.asignar and not folios:
        print("--asignar has no folios. Give at least one invoice folio to assign to, e.g. --asignar A-1006.",
              file=sys.stderr)
        return 1
    accion = "asignar" if folios else args.accion
    rev.anular_conciliacion(args.movimiento, accion, args.usuario, folios, args.comentario or "")
    print(f"Override saved for {args.movimiento}: {accion} {folios}. Run again to recompute.")
    return 0


def cmd_accept(args) -> int:
    """Accept one of the candidates the tool proposed for a receipt (level 2). It is saved as an override."""
    salida, bit, rev = _preparar(args)
    s = _correr(args, bit, rev)
    mov_id = args.movimiento.strip().upper()
    u = next((x for x in s.res.efectivo_no_identificado if x["mov_id"] == mov_id and x.get("nivel") == 2), None)
    if not u:
        print(f"{mov_id} has no proposed candidates to accept (it is already placed, or nothing fits its amount). "
              "Use override --asignar to place it by hand.", file=sys.stderr)
        return 1
    if not 1 <= args.candidato <= len(u["candidatos"]):
        print(f"--candidato must be between 1 and {len(u['candidatos'])} for {mov_id}.", file=sys.stderr)
        return 1
    c = u["candidatos"][args.candidato - 1]
    comentario = (args.comentario or "").strip() or f"accepted proposed candidate {args.candidato}: {c['evidencia']}"
    rev.anular_conciliacion(mov_id, "asignar", args.usuario, [c["folio"]], comentario)
    print(f"{mov_id} assigned to {c['folio']} ({c['cliente']}). Run again to recompute; the tool will learn this payer.")
    return 0


def cmd_rules(args) -> int:
    salida = Path(args.salida)
    reglas = Reglas(salida / "reglas_aprendidas.json", Bitacora(salida / "bitacora_auditoria.jsonl", args.usuario))
    if args.quitar:
        if not (args.usuario or "").strip():
            print("A reviewer name is required (--usuario).", file=sys.stderr)
            return 1
        r = reglas.quitar(args.quitar, args.usuario, args.comentario or "")
        if (salida / "revision.json").exists():   # receipts the rule placed move back: earlier approvals are stale
            Revision(salida / "revision.json", reglas.bitacora).reabrir("learned rule removed " + r["id"])
        print(f"Rule {r['id']} ('{' '.join(r['tokens'])}' -> {r['cliente']}) is no longer used. Run again to recompute.")
        return 0
    if not reglas.reglas:
        print("No learned rules yet. They are created when a reviewer places a receipt whose description names no "
              "invoice and no customer.")
    for r in reglas.reglas:
        estado = "active" if r.get("activa", True) else "inactive"
        print(f"{r['id']} [{estado}] '{' '.join(r['tokens'])}' -> {r['cliente']} (taught by {r['usuario'] or '?'} on "
              f"{r['movimiento']}, {r['ts'][:10]})")
    return 0


def cmd_remove_override(args) -> int:
    salida, bit, rev = _preparar(args)
    mov = str(args.movimiento).strip().upper()
    if mov not in rev.datos["conciliacion"]:
        print(f"There is no override of {mov}. Active overrides: {', '.join(rev.datos['conciliacion']) or 'none'}.",
              file=sys.stderr)
        return 1
    rev.quitar_anulacion(mov, args.usuario)
    print(f"Override of {mov} removed; every approval is pending again. Run again to recompute.")
    return 0


def cmd_reject_correction(args) -> int:
    salida, bit, rev = _preparar(args)
    rev.rechazar_correccion(args.folio, args.usuario, args.comentario or "")
    print(f"Conditional correction for {args.folio} rejected. Run again to recompute.")
    return 0


def cmd_ui(args) -> int:
    import subprocess
    app = Path(__file__).with_name("app.py")
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app), "--", "--datos", str(args.datos or ""),
                            "--salida", str(args.salida)])


def _fecha_cierre(texto: str) -> str:
    try:
        date.fromisoformat(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{texto}' is not a date in the form YYYY-MM-DD (for example 2026-09-30)")
    return texto


def construir_parser():
    p = argparse.ArgumentParser(prog="cierre_cxc", description="Mexican AR close: cash application, exceptions, entries, aging and IVA.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def comunes(sp, datos=True):
        if datos:
            sp.add_argument("--datos", required=True, help="Path to the .xlsx with the dataset")
        sp.add_argument("--salida", default="salida", help="Output folder (default: salida)")
        sp.add_argument("--usuario", default=None, help="Your name, recorded in the audit log")
        sp.add_argument("--cierre", default=None, type=_fecha_cierre,
                        help="Period end YYYY-MM-DD (default: end of the month with most bank rows)")
        sp.add_argument("--llm", action="store_true", help="Let a language model suggest customers for unidentified receipts")

    r = sub.add_parser("run", help="Run the close and write Excel + CSV"); comunes(r); r.set_defaults(fn=cmd_run)
    a = sub.add_parser("approve", help="Approve (or reject) items for review"); comunes(a)
    a.add_argument("--items", default="all", help="'all', a prefix (match:, exc:, pol:) or an exact item key")
    a.add_argument("--estado", default="aprobado", choices=["aprobado", "rechazado", "pendiente"])
    a.add_argument("--comentario", default=""); a.set_defaults(fn=cmd_approve)
    o = sub.add_parser("override", help="Override how a bank movement is applied"); comunes(o)
    o.add_argument("movimiento", help="Movement id, e.g. B13")
    o.add_argument("--asignar", default="", help="Comma-separated invoice folios to apply it to")
    o.add_argument("--accion", default="no_identificado", choices=["no_cxc", "no_identificado"])
    o.add_argument("--comentario", default=""); o.set_defaults(fn=cmd_override)
    ac = sub.add_parser("accept", help="Accept a candidate the tool proposed for a receipt (level 2)"); comunes(ac)
    ac.add_argument("movimiento", help="Movement id, e.g. B08")
    ac.add_argument("--candidato", type=int, default=1, help="Candidate number as listed by run (default 1, the best)")
    ac.add_argument("--comentario", default=""); ac.set_defaults(fn=cmd_accept)
    ro = sub.add_parser("remove-override", help="Remove a reviewer's override of a bank movement"); comunes(ro)
    ro.add_argument("movimiento", help="Movement id, e.g. B13"); ro.set_defaults(fn=cmd_remove_override)
    rl = sub.add_parser("rules", help="List the learned rules, or stop using one")
    rl.add_argument("--salida", default="salida", help="Output folder (default: salida)")
    rl.add_argument("--usuario", default=None, help="Your name, recorded in the audit log")
    rl.add_argument("--quitar", default="", help="Rule id to stop using, e.g. R01")
    rl.add_argument("--comentario", default=""); rl.set_defaults(fn=cmd_rules)
    c = sub.add_parser("reject-correction", help="Reject a conditional rate-correction entry"); comunes(c)
    c.add_argument("folio"); c.add_argument("--comentario", default=""); c.set_defaults(fn=cmd_reject_correction)
    u = sub.add_parser("ui", help="Open the review interface in the browser")
    u.add_argument("--datos", default=None); u.add_argument("--salida", default="salida"); u.set_defaults(fn=cmd_ui)
    return p


def main(argv=None) -> int:
    args = construir_parser().parse_args(argv)
    if getattr(args, "cmd", "") in ("approve", "override", "remove-override", "accept", "reject-correction") and not (args.usuario or "").strip():
        print("A reviewer name is required (--usuario).", file=sys.stderr)
        return 1
    try:
        return args.fn(args)
    except (SchemaError, RevisionError) as e:
        print(f"Input error: {e}", file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"Cannot write {Path(e.filename).name if e.filename else 'an output file'}: it is open in another "
              "program (probably Excel). Close it and run again.", file=sys.stderr)
        return 1
    except OSError as e:   # e.g. --salida points to a file, or a path that cannot be created
        print(f"Cannot use the output folder '{args.salida}': {e.strerror or e}. Give a folder path with --salida.",
              file=sys.stderr)
        return 1
