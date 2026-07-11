#!/usr/bin/env python3
"""Generate a provider-enabled KaroX launcher from start.core.*."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PATCHER_VERSION = "3.12.2"
UTF8_BOM = b"\xef\xbb\xbf"


def one(source: str, old: str, new: str, name: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{name}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def patch_ps(source: str, root: str) -> str:
    escaped_root = root.replace("'", "''")
    source = one(
        source,
        '$Root = Split-Path -Parent $MyInvocation.MyCommand.Path',
        f"$Root = if ($env:KAROX_SOURCE_ROOT) {{ $env:KAROX_SOURCE_ROOT }} else {{ '{escaped_root}' }}",
        "ps root",
    )
    source = one(
        source,
        '    if ($value -in @("letaido")) { return "letaido" }',
        '    if ($value -in @("notion", "notion-ai", "notion_agent")) { return "notion" }\n'
        '    if ($value -in @("letaido")) { return "letaido" }',
        "ps normalize",
    )
    source = one(
        source,
        '    if ($client -eq "letaido") { return "letaido.com" }',
        '    if ($client -eq "notion") { return "Notion Custom Agent" }\n'
        '    if ($client -eq "letaido") { return "letaido.com" }',
        "ps label",
    )
    source = one(
        source,
        'function Get-SelectedAiClient {\n    $settings = Load-Settings',
        'function Get-SelectedAiClient {\n'
        '    if ($env:KAROX_FORCE_AI_CLIENT) { return (Normalize-AiClient $env:KAROX_FORCE_AI_CLIENT) }\n'
        '    $settings = Load-Settings',
        "ps override",
    )
    old_menu = '''    UI-Choice "1" "PROMPTQL" (L "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется") "Magenta"\n    UI-Choice "2" (L "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ") (L "Generic OpenAPI connection" "Универсальное OpenAPI-подключение") "Cyan"\n    UI-Choice "3" "LETAIDO.COM" (L "Compatibility mode" "Режим совместимости") "DarkGray"'''
    new_menu = '''    UI-Choice "1" "PROMPTQL" (L "Native shared AI workspace · recommended" "Нативная командная AI-среда · рекомендуется") "Magenta"\n    UI-Choice "2" "NOTION" (L "Custom Agent over protected MCP" "Custom Agent через защищённый MCP") "Green"\n    UI-Choice "3" (L "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ") (L "Generic OpenAPI connection" "Универсальное OpenAPI-подключение") "Cyan"\n    UI-Choice "4" "LETAIDO.COM" (L "Compatibility mode" "Режим совместимости") "DarkGray"'''
    source = one(source, old_menu, new_menu, "ps menu")
    source = one(
        source,
        '    if ($choice -eq "2") { return "other" }\n'
        '    if ($choice -eq "3") { return "letaido" }\n'
        '    throw (L "Choose an option from 1 to 3." "Выберите вариант от 1 до 3.")',
        '    if ($choice -eq "2") { return "notion" }\n'
        '    if ($choice -eq "3") { return "other" }\n'
        '    if ($choice -eq "4") { return "letaido" }\n'
        '    throw (L "Choose an option from 1 to 4." "Выберите вариант от 1 до 4.")',
        "ps choices",
    )
    source = one(
        source,
        '            tunnelProvider = (Normalize-TunnelProvider $session.tunnelProvider)\n'
        '            tunnelUrl = [string]$session.tunnelUrl',
        '            tunnelProvider = (Normalize-TunnelProvider $session.tunnelProvider)\n'
        '            aiClient = (Normalize-AiClient $session.aiClient)\n'
        '            tunnelUrl = [string]$session.tunnelUrl',
        "ps session",
    )
    notion_ru = '''        } elseif ($aiClient -eq "notion") {\n            $intro = @"\nЯ запустил KaroX для Notion Custom Agent. Добавь custom MCP server:\n- name: KaroX\n- server URL: $tunnelUrl/mcp\n- transport: Streamable HTTP\n- authorization: Bearer token\n- token: ключ из клавиши K, только в защищённое поле\nНе вставляй ключ в чат. Сначала вызови karox_preflight и дождись отдельного ТЗ.\n"@\n        } elseif ($aiClient -eq "letaido") {'''
    notion_en = '''        } elseif ($aiClient -eq "notion") {\n            $intro = @"\nI started KaroX for a Notion Custom Agent. Add a custom MCP server:\n- name: KaroX\n- server URL: $tunnelUrl/mcp\n- transport: Streamable HTTP\n- authorization: Bearer token\n- token: the key copied with K, only in the protected field\nNever paste the key into chat. Call karox_preflight first and wait for a separate task.\n"@\n        } elseif ($aiClient -eq "letaido") {'''
    anchor = '        } elseif ($aiClient -eq "letaido") {'
    first = source.find(anchor)
    if first < 0:
        raise RuntimeError("ps notion ru anchor not found")
    source = source[:first] + notion_ru + source[first + len(anchor):]
    second = source.find(anchor, first + len(notion_ru))
    if second < 0:
        raise RuntimeError("ps notion en anchor not found")
    source = source[:second] + notion_en + source[second + len(anchor):]

    source = one(
        source,
        '        $_.CommandLine -like "*repo_tools:app*" -or',
        '        $_.CommandLine -like "*repo_tools:app*" -or\n'
        '        $_.CommandLine -like "*app_entry:app*" -or\n'
        '        $_.CommandLine -like "*notion_gateway:app*" -or\n'
        '        $_.CommandLine -like "*notion_entry:app*" -or',
        "ps stop",
    )
    source = one(
        source,
        '    $previousPythonPath = $env:PYTHONPATH\n    try {',
        '    $previousPythonPath = $env:PYTHONPATH\n'
        '    $serverApp = if ($aiClient -eq "notion") { "notion_entry:app" } else { "app_entry:app" }\n'
        '    try {',
        "ps app var",
    )
    source = one(source, '"repo_tools:app", "--host"', '$serverApp, "--host"', "ps app target")
    return source


def patch_sh(source: str, root: str) -> str:
    escaped_root = root.replace("'", "'\\''")
    source = one(
        source,
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
        f"SCRIPT_DIR=\"${{KAROX_SOURCE_ROOT:-{escaped_root}}}\"",
        "sh root",
    )
    source = one(
        source,
        "        letaido) printf 'letaido' ;;",
        "        notion|notion-ai|notion_agent) printf 'notion' ;;\n        letaido) printf 'letaido' ;;",
        "sh normalize",
    )
    source = one(
        source,
        "        letaido) printf 'letaido.com' ;;",
        "        notion) printf 'Notion Custom Agent' ;;\n        letaido) printf 'letaido.com' ;;",
        "sh label",
    )
    source = one(
        source,
        'get_selected_ai_client() {\n    local s;',
        'get_selected_ai_client() {\n'
        '    if [ -n "${KAROX_FORCE_AI_CLIENT:-}" ]; then normalize_ai_client "$KAROX_FORCE_AI_CLIENT"; return 0; fi\n'
        '    local s;',
        "sh override",
    )
    source = one(
        source,
        '    ui_choice 2 "$(l "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ")"',
        '    ui_choice 2 NOTION "$(l "Custom Agent over protected MCP" "Custom Agent через защищённый MCP")" "$C_GREEN"\n'
        '    ui_choice 3 "$(l "OTHER CLIENT" "ДРУГОЙ КЛИЕНТ")"',
        "sh menu",
    )
    source = one(source, '    ui_choice 3 LETAIDO.COM', '    ui_choice 4 LETAIDO.COM', "sh letaido number")
    source = one(
        source,
        'case "$choice" in 1) printf promptql ;; 2) printf other ;; 3) printf letaido ;; *) return 1 ;; esac',
        'case "$choice" in 1) printf promptql ;; 2) printf notion ;; 3) printf other ;; 4) printf letaido ;; *) return 1 ;; esac',
        "sh choices",
    )
    source = source.replace(
        '            letaido)\n                intro="Я запустил Star For KaroX',
        '            notion)\n                intro="Я запустил KaroX для Notion Custom Agent. Добавь MCP server $tunnel_url/mcp, Streamable HTTP, Bearer token из клавиши K. Сначала вызови karox_preflight и дождись ТЗ." ;;\n'
        '            letaido)\n                intro="Я запустил Star For KaroX',
        1,
    )
    source = source.replace(
        '            letaido)\n                intro="I started Star For KaroX',
        '            notion)\n                intro="I started KaroX for a Notion Custom Agent. Add MCP server $tunnel_url/mcp, Streamable HTTP, Bearer token copied with K. Call karox_preflight first and wait for the task." ;;\n'
        '            letaido)\n                intro="I started Star For KaroX',
        1,
    )
    old_target = '    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn --app-dir "$SERVER_DIR" \\\n        repo_tools:app --host 127.0.0.1 --port "$local_port" \\'
    new_target = '    local server_app="app_entry:app"\n'
    new_target += '    [ "$ai_client" = notion ] && server_app="notion_entry:app"\n'
    new_target += '    (cd "$SERVER_DIR" && nohup "$PYTHON_EXE" -m uvicorn --app-dir "$SERVER_DIR" \\\n        "$server_app" --host 127.0.0.1 --port "$local_port" \\'
    source = one(source, old_target, new_target, "sh app target")
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("powershell", "shell"), required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()

    raw = args.source.read_text(encoding="utf-8-sig")
    root = str(args.root.resolve())
    patched = patch_ps(raw, root) if args.platform == "powershell" else patch_sh(raw, root)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    rendered = (
        f"# KaroX patcher: {PATCHER_VERSION}\n"
        f"# KaroX core sha256: {digest}\n"
        "# Generated; do not edit.\n"
        + patched
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    is_powershell = args.platform == "powershell"
    output_encoding = "utf-8-sig" if is_powershell else "utf-8"
    reused = False
    if args.output.is_file():
        try:
            existing_bytes = args.output.read_bytes()
            bom_is_correct = existing_bytes.startswith(UTF8_BOM) if is_powershell else not existing_bytes.startswith(UTF8_BOM)
            reused = bom_is_correct and existing_bytes.decode(output_encoding) == rendered
        except (OSError, UnicodeError):
            reused = False
    if not reused:
        args.output.write_text(rendered, encoding=output_encoding, newline="\n")
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "coreSha256": digest,
                "patcherVersion": PATCHER_VERSION,
                "encoding": output_encoding,
                "reused": reused,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise
