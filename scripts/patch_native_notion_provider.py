#!/usr/bin/env python3
"""Post-process the generated KaroX launcher with native Notion provider UX."""
from __future__ import annotations

import argparse
from pathlib import Path


UTF8_BOM = b"\xef\xbb\xbf"


def one(source: str, old: str, new: str, name: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{name}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def patch_powershell(source: str) -> str:
    source = one(
        source,
        '$NotionProfileScript = Join-Path $Root "scripts\\notion_profile.py"',
        '$NotionProfileScript = Join-Path $Root "scripts\\notion_profile.py"\n'
        '$NativeNotionProviderScript = Join-Path $Root "scripts\\native_notion_provider.py"',
        "ps native provider path",
    )
    source = one(
        source,
        '    $settings.aiClient = Normalize-AiClient $settings.aiClient\n'
        '    $settings.language = Normalize-Language $settings.language',
        '    $settings.aiClient = Normalize-AiClient $settings.aiClient\n'
        '    if ($settings.aiClient -eq "notion") { $settings.tunnelProvider = "tailscale" }\n'
        '    $settings.language = Normalize-Language $settings.language',
        "ps notion forces tailscale",
    )
    source = one(
        source,
        'function Get-SelectedTunnelProvider {\n'
        '    $settings = Load-Settings\n'
        '    return (Normalize-TunnelProvider $settings.tunnelProvider)\n'
        '}',
        'function Get-SelectedTunnelProvider {\n'
        '    $settings = Load-Settings\n'
        '    if ((Normalize-AiClient $settings.aiClient) -eq "notion") { return "tailscale" }\n'
        '    return (Normalize-TunnelProvider $settings.tunnelProvider)\n'
        '}',
        "ps selected notion tunnel",
    )

    helpers = r'''function Initialize-NativeNotionProvider {
    if (!(Test-Path -LiteralPath $NativeNotionProviderScript)) {
        throw (L "Built-in Notion provider component is missing. Run karox update." "Компонент встроенного провайдера Notion отсутствует. Выполните обновление KaroX.")
    }
    UI-Notice "progress" (L "Preparing built-in Notion provider" "Подготавливаю встроенный провайдер Notion") (L "Tailscale Funnel and the persistent MCP identity are managed automatically." "Tailscale Funnel и постоянное MCP-подключение настраиваются автоматически.")
    & $PythonExe $NativeNotionProviderScript prepare
    if ($LASTEXITCODE -ne 0) {
        throw (L "The built-in Notion provider is not ready." "Встроенный провайдер Notion не готов.")
    }
}

function Show-NativeNotionConnection($tunnelUrl, $apiKey) {
    $mcpUrl = ([string]$tunnelUrl).TrimEnd("/") + "/mcp"
    Write-Host ""
    UI-Section (L "Native Notion connection" "Нативное подключение Notion")
    UI-Notice "success" (L "NOTION PROVIDER IS LIVE" "ПРОВАЙДЕР NOTION ЗАПУЩЕН") (L "The MCP server is already running. Keep this session open while Notion works." "MCP-сервер уже работает. Не закрывайте сессию, пока Notion работает.")
    UI-KeyValue "MCP server URL" $mcpUrl "Cyan"
    UI-KeyValue (L "Authentication" "Авторизация") "Bearer token"
    UI-KeyValue (L "Prefix" "Префикс") "Bearer"
    Write-Host ""
    UI-Choice "U" (L "COPY URL" "СКОПИРОВАТЬ URL") (L "Paste it into MCP server URL in Notion" "Вставьте в поле MCP server URL в Notion") "Cyan"
    UI-Choice "K" (L "COPY TOKEN" "СКОПИРОВАТЬ ТОКЕН") (L "Paste only into Notion's protected Token field" "Вставьте только в защищённое поле Token в Notion") "Yellow"
    Write-Host ("  │  [Enter] " + (L "Continue to session controls" "Перейти к управлению сессией")) -ForegroundColor DarkGray
    while ($true) {
        $choice = ([string](Read-Host ("  › " + (L "Action" "Действие")))).Trim()
        if ($choice -match "^[Uu]$") {
            Set-Clipboard -Value $mcpUrl
            UI-Notice "success" (L "MCP URL copied" "MCP URL скопирован") $mcpUrl
            continue
        }
        if ($choice -match "^[Kk]$") {
            Set-Clipboard -Value $apiKey
            UI-Notice "success" (L "Token copied" "Токен скопирован") (L "Paste it only into Notion's protected Token field." "Вставьте его только в защищённое поле Token в Notion.")
            continue
        }
        return
    }
}

'''
    source = one(
        source,
        "function Get-PersistentNotionProfile {",
        helpers + "function Get-PersistentNotionProfile {",
        "ps native provider helpers",
    )
    source = one(
        source,
        '    if ($aiClient -eq "notion") {\n'
        '        Stop-OtherNotionSessions',
        '    if ($aiClient -eq "notion") {\n'
        '        Initialize-NativeNotionProvider\n'
        '        Stop-OtherNotionSessions',
        "ps native provider prepare",
    )
    source = one(
        source,
        '    Write-Host ((L "Session folder: " "Папка сессии: ") + $sessionDir)\n'
        '    return (Load-SessionJson $sessionDir)',
        '    Write-Host ((L "Session folder: " "Папка сессии: ") + $sessionDir)\n'
        '    if ($aiClient -eq "notion") { Show-NativeNotionConnection $tunnelUrl $apiKey }\n'
        '    return (Load-SessionJson $sessionDir)',
        "ps native connection card",
    )
    source = one(
        source,
        '        UI-Choice "A" (L "HANDOFF" "ПЕРЕДАТЬ") (L "Copy the complete connection package" "Скопировать полный пакет подключения") "Magenta"\n'
        '        Write-Host ("  │  [C] " + (L "Connect" "Подключение") + "   [T] " + (L "Task" "Задача") + "   [K] " + (L "Key" "Ключ") + "   [P] Provider ID") -ForegroundColor DarkGray',
        '        UI-Choice "A" (L "HANDOFF" "ПЕРЕДАТЬ") (L "Copy the complete connection package" "Скопировать полный пакет подключения") "Magenta"\n'
        '        if ((Normalize-AiClient $session.aiClient) -eq "notion") { UI-Choice "N" "NOTION" (L "Open the native Notion connection card" "Открыть карточку подключения Notion") "Green" }\n'
        '        Write-Host ("  │  [C] " + (L "Connect" "Подключение") + "   [T] " + (L "Task" "Задача") + "   [K] " + (L "Key" "Ключ") + "   [P] Provider ID") -ForegroundColor DarkGray',
        "ps notion session choice",
    )
    source = one(
        source,
        '        elseif ($x -match "^[Aa]$") { Copy-SessionFile $session "all.txt" (L "Complete handoff copied." "Полный handoff скопирован.") }\n'
        '        elseif ($x -match "^[Ll]$") { Show-LogTail $session }',
        '        elseif ($x -match "^[Aa]$") { Copy-SessionFile $session "all.txt" (L "Complete handoff copied." "Полный handoff скопирован.") }\n'
        '        elseif ($x -match "^[Nn]$" -and (Normalize-AiClient $session.aiClient) -eq "notion") { Show-NativeNotionConnection $session.tunnelUrl $session.apiKey }\n'
        '        elseif ($x -match "^[Ll]$") { Show-LogTail $session }',
        "ps notion session handler",
    )
    return source


def patch_shell(source: str) -> str:
    source = one(
        source,
        'NOTION_PROFILE_SCRIPT="$SCRIPT_DIR/scripts/notion_profile.py"',
        'NOTION_PROFILE_SCRIPT="$SCRIPT_DIR/scripts/notion_profile.py"\n'
        'NATIVE_NOTION_PROVIDER_SCRIPT="$SCRIPT_DIR/scripts/native_notion_provider.py"',
        "sh native provider path",
    )
    source = one(
        source,
        '    [ -n "$language" ] || language="$(json_py "import json,sys; print(json.loads(sys.stdin.read() or \'{}\').get(\'language\',\'en\'))" <<< "$current" 2>/dev/null || echo en)"\n'
        '    mkdir -p "$CONFIG_DIR"',
        '    [ -n "$language" ] || language="$(json_py "import json,sys; print(json.loads(sys.stdin.read() or \'{}\').get(\'language\',\'en\'))" <<< "$current" 2>/dev/null || echo en)"\n'
        '    ai_client="$(normalize_ai_client "$ai_client")"\n'
        '    [ "$ai_client" = notion ] && tunnel=tailscale\n'
        '    mkdir -p "$CONFIG_DIR"',
        "sh notion forces tailscale",
    )
    source = one(
        source,
        'get_selected_tunnel_provider() {\n'
        '    local s; s="$(load_settings)"\n'
        '    printf \'%s\' "$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get(\'tunnelProvider\',\'cloudflare\'))" <<< "$s" 2>/dev/null || echo cloudflare)"\n'
        '}',
        'get_selected_tunnel_provider() {\n'
        '    local s selected_ai; s="$(load_settings)"\n'
        '    selected_ai="$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get(\'aiClient\',\'promptql\'))" <<< "$s" 2>/dev/null || echo promptql)"\n'
        '    [ "$(normalize_ai_client "$selected_ai")" = notion ] && { printf tailscale; return 0; }\n'
        '    printf \'%s\' "$(json_py "import json,sys; print(json.loads(sys.stdin.read()).get(\'tunnelProvider\',\'cloudflare\'))" <<< "$s" 2>/dev/null || echo cloudflare)"\n'
        '}',
        "sh selected notion tunnel",
    )

    helpers = r'''initialize_native_notion_provider() {
    [ -f "$NATIVE_NOTION_PROVIDER_SCRIPT" ] || { log_error "$(l "Built-in Notion provider component is missing. Run karox update." "Компонент встроенного провайдера Notion отсутствует. Выполните обновление KaroX.")"; return 1; }
    ui_notice progress "$(l "Preparing built-in Notion provider" "Подготавливаю встроенный провайдер Notion")" "$(l "Tailscale Funnel and the persistent MCP identity are managed automatically." "Tailscale Funnel и постоянное MCP-подключение настраиваются автоматически.")"
    "$PYTHON_EXE" "$NATIVE_NOTION_PROVIDER_SCRIPT" prepare
}

show_native_notion_connection() {
    local tunnel_url="$1" api_key="$2" mcp_url="${1%/}/mcp" choice
    echo ""
    ui_section "$(l "Native Notion connection" "Нативное подключение Notion")"
    ui_notice success "$(l "NOTION PROVIDER IS LIVE" "ПРОВАЙДЕР NOTION ЗАПУЩЕН")" "$(l "The MCP server is already running. Keep this session open while Notion works." "MCP-сервер уже работает. Не закрывайте сессию, пока Notion работает.")"
    ui_kv "MCP server URL" "$mcp_url"
    ui_kv "$(l "Authentication" "Авторизация")" "Bearer token"
    ui_kv "$(l "Prefix" "Префикс")" "Bearer"
    echo ""
    ui_choice U "$(l "COPY URL" "СКОПИРОВАТЬ URL")" "$(l "Paste it into MCP server URL in Notion" "Вставьте в поле MCP server URL в Notion")" "$C_CYAN"
    ui_choice K "$(l "COPY TOKEN" "СКОПИРОВАТЬ ТОКЕН")" "$(l "Paste only into Notion's protected Token field" "Вставьте только в защищённое поле Token в Notion")" "$C_YELLOW"
    printf '%s  │  [Enter] %s%s\n' "$C_DARK" "$(l "Continue to session controls" "Перейти к управлению сессией")" "$C_RESET"
    while true; do
        printf '  › %s: ' "$(l "Action" "Действие")" >&2
        read -r choice
        case "$choice" in
            [Uu]) printf '%s' "$mcp_url" | copy_to_clipboard >/dev/null 2>&1 && log_success "$(l "MCP URL copied." "MCP URL скопирован.")" || log_warn "$mcp_url" ;;
            [Kk]) printf '%s' "$api_key" | copy_to_clipboard >/dev/null 2>&1 && log_success "$(l "Token copied. Paste it only into Notion's protected Token field." "Токен скопирован. Вставьте его только в защищённое поле Token в Notion.")" || log_warn "$(l "Clipboard unavailable." "Буфер обмена недоступен.")" ;;
            *) return 0 ;;
        esac
    done
}

'''
    source = one(
        source,
        "persistent_notion_profile_json() {",
        helpers + "persistent_notion_profile_json() {",
        "sh native provider helpers",
    )
    source = one(
        source,
        '    if [ "$ai_client" = notion ]; then\n'
        '        stop_other_notion_sessions',
        '    if [ "$ai_client" = notion ]; then\n'
        '        initialize_native_notion_provider || return 1\n'
        '        stop_other_notion_sessions',
        "sh native provider prepare",
    )
    source = one(
        source,
        '    echo "$(l "Session folder" "Папка сессии"): $session_dir"\n'
        '    # Возвращаем session_dir для вызывающего меню.\n'
        '    printf \'%s\' "$session_dir"',
        '    echo "$(l "Session folder" "Папка сессии"): $session_dir"\n'
        '    [ "$ai_client" = notion ] && show_native_notion_connection "$tunnel_url" "$api_key"\n'
        '    # Возвращаем session_dir для вызывающего меню.\n'
        '    printf \'%s\' "$session_dir"',
        "sh native connection card",
    )
    source = one(
        source,
        '        ui_choice A "$(l "HANDOFF" "ПЕРЕДАТЬ")" "$(l "Copy the complete connection package" "Скопировать полный пакет подключения")" "$C_MAGENTA"\n'
        '        printf \'%s  │  [C] %s   [T] %s   [K] %s   [P] Provider ID%s\\n\' "$C_DARK" "$(l "Connect" "Подключение")" "$(l "Task" "Задача")" "$(l "Key" "Ключ")" "$C_RESET"',
        '        ui_choice A "$(l "HANDOFF" "ПЕРЕДАТЬ")" "$(l "Copy the complete connection package" "Скопировать полный пакет подключения")" "$C_MAGENTA"\n'
        '        [ "$(normalize_ai_client "$ac")" = notion ] && ui_choice N NOTION "$(l "Open the native Notion connection card" "Открыть карточку подключения Notion")" "$C_GREEN"\n'
        '        printf \'%s  │  [C] %s   [T] %s   [K] %s   [P] Provider ID%s\\n\' "$C_DARK" "$(l "Connect" "Подключение")" "$(l "Task" "Задача")" "$(l "Key" "Ключ")" "$C_RESET"',
        "sh notion session choice",
    )
    source = one(
        source,
        '            [Aa]) copy_session_file "$session_dir" all.txt "$(l "Complete handoff copied." "Полный handoff скопирован.")" ;;\n'
        '            [Ll]) show_log_tail "$session_json" ;;',
        '            [Aa]) copy_session_file "$session_dir" all.txt "$(l "Complete handoff copied." "Полный handoff скопирован.")" ;;\n'
        '            [Nn]) [ "$(normalize_ai_client "$ac")" = notion ] && show_native_notion_connection "$url" "$(json_py "import json,sys; print(json.load(sys.stdin).get(\'apiKey\',\'\'))" <<< "$session_json")" ;;\n'
        '            [Ll]) show_log_tail "$session_json" ;;',
        "sh notion session handler",
    )
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("powershell", "shell"), required=True)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()

    encoding = "utf-8-sig" if args.platform == "powershell" else "utf-8"
    source = args.path.read_text(encoding=encoding)
    patched = patch_powershell(source) if args.platform == "powershell" else patch_shell(source)
    args.path.write_text(patched, encoding=encoding, newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
