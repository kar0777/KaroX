#!/usr/bin/env python3
"""Generate a Notion-enabled KaroX launcher from start.core.*."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path


def one(s: str, old: str, new: str, name: str) -> str:
    if s.count(old) != 1:
        raise RuntimeError(f"{name}: expected one anchor, found {s.count(old)}")
    return s.replace(old, new, 1)


def patch_ps(s: str, root: str) -> str:
    r = root.replace("'", "''")
    s = one(s, '$Root = Split-Path -Parent $MyInvocation.MyCommand.Path',
            f"$Root = if ($env:KAROX_SOURCE_ROOT) {{ $env:KAROX_SOURCE_ROOT }} else {{ '{r}' }}", 'ps root')
    s = one(s, '    if ($value -in @("letaido")) { return "letaido" }',
            '    if ($value -in @("notion", "notion-ai", "notion_agent")) { return "notion" }\n    if ($value -in @("letaido")) { return "letaido" }', 'ps normalize')
    s = one(s, '    if ($client -eq "letaido") { return "letaido.com" }',
            '    if ($client -eq "notion") { return "Notion Custom Agent" }\n    if ($client -eq "letaido") { return "letaido.com" }', 'ps label')
    s = one(s, 'function Get-SelectedAiClient {\n    $settings = Load-Settings',
            'function Get-SelectedAiClient {\n    if ($env:KAROX_FORCE_AI_CLIENT) { return (Normalize-AiClient $env:KAROX_FORCE_AI_CLIENT) }\n    $settings = Load-Settings', 'ps override')
    old = '''    UI-Choice "1" "PROMPTQL" (L "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется") "Magenta"\n    UI-Choice "2" (L "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ") (L "Generic OpenAPI connection" "Универсальное OpenAPI-подключение") "Cyan"\n    UI-Choice "3" "LETAIDO.COM" (L "Compatibility mode" "Режим совместимости") "DarkGray"'''
    new = '''    UI-Choice "1" "PROMPTQL" (L "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется") "Magenta"\n    UI-Choice "2" "NOTION" (L "Custom Agent over protected MCP" "Custom Agent через защищённый MCP") "Green"\n    UI-Choice "3" (L "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ") (L "Generic OpenAPI connection" "Универсальное OpenAPI-подключение") "Cyan"\n    UI-Choice "4" "LETAIDO.COM" (L "Compatibility mode" "Режим совместимости") "DarkGray"'''
    s = one(s, old, new, 'ps menu')
    s = one(s, '    if ($choice -eq "2") { return "other" }\n    if ($choice -eq "3") { return "letaido" }\n    throw (L "Choose an option from 1 to 3." "Выберите вариант от 1 до 3.")',
            '    if ($choice -eq "2") { return "notion" }\n    if ($choice -eq "3") { return "other" }\n    if ($choice -eq "4") { return "letaido" }\n    throw (L "Choose an option from 1 to 4." "Выберите вариант от 1 до 4.")', 'ps choices')
    s = one(s, '            tunnelProvider = (Normalize-TunnelProvider $session.tunnelProvider)\n            tunnelUrl = [string]$session.tunnelUrl',
            '            tunnelProvider = (Normalize-TunnelProvider $session.tunnelProvider)\n            aiClient = (Normalize-AiClient $session.aiClient)\n            tunnelUrl = [string]$session.tunnelUrl', 'ps session')
    notion_ru = '''        } elseif ($aiClient -eq "notion") {\n            $intro = @"\nЯ запустил KaroX для Notion Custom Agent. Добавь custom MCP server:\n- name: KaroX\n- server URL: $tunnelUrl/mcp\n- transport: Streamable HTTP\n- authorization: Bearer token\n- token: ключ из клавиши K, только в защищённое поле\nНе вставляй ключ в чат. Сначала вызови karox_preflight и дождись отдельного ТЗ.\n"@\n        } elseif ($aiClient -eq "letaido") {'''
    notion_en = '''        } elseif ($aiClient -eq "notion") {\n            $intro = @"\nI started KaroX for a Notion Custom Agent. Add a custom MCP server:\n- name: KaroX\n- server URL: $tunnelUrl/mcp\n- transport: Streamable HTTP\n- authorization: Bearer token\n- token: the key copied with K, only in the protected field\nNever paste the key into chat. Call karox_preflight first and wait for a separate task.\n"@\n        } elseif ($aiClient -eq "letaido") {'''
    anchor = '        } elseif ($aiClient -eq "letaido") {'
    pos = s.find(anchor); s = s[:pos] + notion_ru + s[pos+len(anchor):]
    pos = s.find(anchor, pos + len(notion_ru)); s = s[:pos] + notion_en + s[pos+len(anchor):]
    s = one(s, '        $_.CommandLine -like "*repo_tools:app*" -or',
            '        $_.CommandLine -like "*repo_tools:app*" -or\n        $_.CommandLine -like "*notion_gateway:app*" -or', 'ps stop')
    s = one(s, '    $previousPythonPath = $env:PYTHONPATH\n    try {',
            '    $previousPythonPath = $env:PYTHONPATH\n    $serverApp = if ($aiClient -eq "notion") { "notion_gateway:app" } else { "repo_tools:app" }\n    try {', 'ps app var')
    s = one(s, '"repo_tools:app", "--host"', '$serverApp, "--host"', 'ps app target')
    return s


def patch_sh(s: str, root: str) -> str:
    r = root.replace("'", "'\\''")
    s = one(s, 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"', f'SCRIPT_DIR="${{KAROX_SOURCE_ROOT:-{r}}}"', 'sh root')
    s = one(s, "        letaido) printf 'letaido' ;;", "        notion|notion-ai|notion_agent) printf 'notion' ;;\n        letaido) printf 'letaido' ;;", 'sh normalize')
    s = one(s, "        letaido) printf 'letaido.com' ;;", "        notion) printf 'Notion Custom Agent' ;;\n        letaido) printf 'letaido.com' ;;", 'sh label')
    s = one(s, 'get_selected_ai_client() {\n    local s;', 'get_selected_ai_client() {\n    if [ -n "${KAROX_FORCE_AI_CLIENT:-}" ]; then normalize_ai_client "$KAROX_FORCE_AI_CLIENT"; return 0; fi\n    local s;', 'sh override')
    s = one(s, '    ui_choice 2 "$(l "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ")"', '    ui_choice 2 NOTION "$(l "Custom Agent over protected MCP" "Custom Agent через защищённый MCP")" "$C_GREEN"\n    ui_choice 3 "$(l "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ")"', 'sh menu')
    s = one(s, '    ui_choice 3 LETAIDO.COM', '    ui_choice 4 LETAIDO.COM', 'sh letaido number')
    s = one(s, 'case "$choice" in 1) printf promptql ;; 2) printf other ;; 3) printf letaido ;; *) return 1 ;; esac', 'case "$choice" in 1) printf promptql ;; 2) printf notion ;; 3) printf other ;; 4) printf letaido ;; *) return 1 ;; esac', 'sh choices')
    s = s.replace('            letaido)\n                intro="Я запустил Star For KaroX', '            notion)\n                intro="Я запустил KaroX для Notion Custom Agent. Добавь MCP server $tunnel_url/mcp, Streamable HTTP, Bearer token из клавиши K. Сначала вызови karox_preflight и дождись ТЗ." ;;\n            letaido)\n                intro="Я запустил Star For KaroX', 1)
    s = s.replace('            letaido)\n                intro="I started Star For KaroX', '            notion)\n                intro="I started KaroX for a Notion Custom Agent. Add MCP server $tunnel_url/mcp, Streamable HTTP, Bearer token copied with K. Call karox_preflight first and wait for the task." ;;\n            letaido)\n                intro="I started Star For KaroX', 1)
    old = '    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn --app-dir "$SERVER_DIR" \\\n        repo_tools:app --host 127.0.0.1 --port "$local_port" \\'
    new = '    local server_app="repo_tools:app"\n    [ "$ai_client" = notion ] && server_app="notion_gateway:app"\n    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn --app-dir "$SERVER_DIR" \\\n        "$server_app" --host 127.0.0.1 --port "$local_port" \\'
    s = one(s, old, new, 'sh app target')
    return s


def main() -> int:
    p = argparse.ArgumentParser(); p.add_argument('--platform', choices=('powershell','shell'), required=True); p.add_argument('--source', type=Path, required=True); p.add_argument('--output', type=Path, required=True); p.add_argument('--root', type=Path, required=True); a = p.parse_args()
    raw = a.source.read_text(encoding='utf-8-sig'); root = str(a.root.resolve())
    out = patch_ps(raw, root) if a.platform == 'powershell' else patch_sh(raw, root)
    digest = hashlib.sha256(raw.encode()).hexdigest(); a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(f'# KaroX core sha256: {digest}\n# Generated; do not edit.\n' + out, encoding='utf-8', newline='\n')
    print(json.dumps({'ok':True,'output':str(a.output),'coreSha256':digest})); return 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as e: print(json.dumps({'ok':False,'error':str(e)})); raise
