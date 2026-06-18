import sys, paramiko, os
host = "92.113.151.67"
user = "pantomas"
password = "babajaga"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False)

if len(sys.argv) > 1 and sys.argv[1] == "sync-example":
    with open(r"D:\kody\backend-AI\.env.example", "r") as f:
        content = f.read()
    sftp = client.open_sftp()
    with sftp.file("/opt/ai-backend/.env.example", "w") as f:
        f.write(content)
    sftp.close()
    print(".env.example synced to VPS")
else:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "hostname"
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out:
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.flush()
    if err:
        sys.stderr.buffer.write(f"STDERR: {err}".encode("utf-8"))
        sys.stderr.flush()
client.close()
