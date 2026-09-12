' Runs run_daily.bat without a console window, so a scheduled run cannot be killed by closing a window
' (two runs ended with a Ctrl+C exit code that way). Usage: wscript.exe run_hidden.vbs [meta]
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
bat = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "run_daily.bat")
mode = ""
If WScript.Arguments.Count > 0 Then mode = " " & WScript.Arguments(0)
sh.Run "cmd.exe /c """ & bat & """" & mode, 0, True
