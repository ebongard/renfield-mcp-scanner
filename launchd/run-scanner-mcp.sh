#!/usr/bin/env bash
#
# LaunchAgent entrypoint. Pulls both secrets from the macOS Keychain at start,
# so neither appears in the plist — a plist is world-readable.
#
# Two DIFFERENT secrets, protecting opposite directions:
#   SCANNER_TOKEN_PRIMARY  this server -> Renfield  (pushing documents)
#   SCANNER_MCP_TOKEN      Renfield -> this server  (calling tools)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

kc() { security find-generic-password -a renfield-scanner -s "$1" -w 2>/dev/null; }

export SCANNER_TARGETS_YAML="${SCANNER_TARGETS_YAML:-$HERE/targets.yaml}"
export SCANNER_TOKEN_PRIMARY="$(kc SCANNER_TOKEN_PRIMARY)"
# EIN Token je Aufrufer: eine Instanz laesst sich widerrufen, ohne die andere
# zu stoeren — derselbe Grund wie beim Push-Token je Ziel.
export SCANNER_MCP_TOKEN_HOUSEHOLD="$(kc SCANNER_MCP_TOKEN_HOUSEHOLD)"
export SCANNER_MCP_TOKEN_XIDRA="$(kc SCANNER_MCP_TOKEN_XIDRA)"
# Rueckkanal fuer Scan-Job-Ergebnisse: welcher Aufrufer welches Ziel IST. Das
# Ergebnis geht an die Instanz, die den Scan angefordert hat — ueber deren
# base_url und Push-Token. Kein neues Geheimnis; ohne Zuordnung meldet niemand.
export SCANNER_CALLER_TARGET_HOUSEHOLD="${SCANNER_CALLER_TARGET_HOUSEHOLD:-household}"
export SCANNER_CALLER_TARGET_XIDRA="${SCANNER_CALLER_TARGET_XIDRA:-xidra}"
# Wohin die FERTIGMELDUNG eines knopfgestarteten Scans geht. Ohne diese Zeile
# laeuft der Scan zwar durch und das Dokument wird zugestellt, aber niemand
# erfaehrt davon: "outcome done is recorded but nobody is told" (2026-09-25).
# Betrifft NUR die Meldung — wohin das DOKUMENT geht, entscheidet weiterhin der
# Klassifizierer, und ein Trennblatt sticht ihn aus.
export SCANNER_CALLER_TARGET_BUTTON="${SCANNER_CALLER_TARGET_BUTTON:-household}"
# Ein Push-Token je Ziel. Ein fehlendes laesst NUR dieses Ziel ausfallen —
# der Scanner meldet es beim Preflight namentlich, statt still zu scheitern.
export SCANNER_TOKEN_XIDRA="$(kc SCANNER_TOKEN_XIDRA)"
# L3 wird erst ab dem zweiten Ziel relevant: bei einem greift der Kurzschluss.
export SCANNER_CLASSIFIER_URL="${SCANNER_CLASSIFIER_URL:-http://cuda.local:8081/v1}"
export SCANNER_CLASSIFIER_MODEL="${SCANNER_CLASSIFIER_MODEL:-qwen3.6}"
# 0.0.0.0 so the in-cluster backend can reach it. The server REFUSES to bind a
# non-loopback address unless SCANNER_MCP_TOKEN is set, so a Keychain miss fails
# closed rather than silently exposing the endpoint.
export SCANNER_MCP_HOST="${SCANNER_MCP_HOST:-0.0.0.0}"
export SCANNER_MCP_PORT="${SCANNER_MCP_PORT:-9093}"

[ -n "$SCANNER_TOKEN_PRIMARY" ] || { echo "SCANNER_TOKEN_PRIMARY not in Keychain" >&2; exit 1; }
[ -n "$SCANNER_MCP_TOKEN_HOUSEHOLD$SCANNER_MCP_TOKEN_XIDRA" ] || { echo "kein SCANNER_MCP_TOKEN_<CALLER> im Schluesselbund" >&2; exit 1; }

exec "$HERE/.venv/bin/renfield-mcp-scanner"
