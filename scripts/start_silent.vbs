' Launch the uploader server with no visible console window.
' Edit PY_EXE below if your venv lives elsewhere.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
venvPy    = projectDir & "\.venv\Scripts\python.exe"
logsDir   = projectDir & "\logs"
logFile   = logsDir & "\launcher.log"

If Not fso.FolderExists(logsDir) Then
    fso.CreateFolder(logsDir)
End If

Set log = fso.OpenTextFile(logFile, 8, True)

If Not fso.FileExists(venvPy) Then
    log.WriteLine Now & " ERROR Missing venv Python launcher: " & venvPy
    log.Close
    WScript.Quit 1
End If

cmd = """" & venvPy & """ -m uploader"
sh.CurrentDirectory = projectDir

On Error Resume Next
rc = sh.Run(cmd, 0, False)
If Err.Number <> 0 Then
    log.WriteLine Now & " ERROR Failed to start uploader: " & Err.Description
    log.Close
    WScript.Quit Err.Number
End If
On Error GoTo 0

log.WriteLine Now & " INFO Launch requested: " & cmd
log.Close
