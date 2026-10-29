Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d [directory] && ""[python-executable-path]"" ""[archive\bot_server.py path]"" >> ""[bot_server_log.log path]"" 2>&1", 0, False
Set WshShell = Nothing