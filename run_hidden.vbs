' Runs the .bat file passed as the first argument with its console window
' hidden (WindowStyle 0), waiting for it to finish (True) so Task Scheduler
' still correctly reports success/failure and duration. Task Scheduler
' launching a .bat directly always shows a console window while it runs —
' this wrapper is the standard way to suppress that without changing the
' task's logon type (which would need storing a Windows password).
Set objShell = CreateObject("WScript.Shell")
objShell.Run """" & WScript.Arguments(0) & """", 0, True
