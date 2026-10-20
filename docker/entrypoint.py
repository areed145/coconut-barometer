"""Container entrypoint: give the data disk to the unprivileged `app` user, then run the
command as that user. Starts as root only for this; a mounted disk may arrive root-owned."""
import os
import pwd
import sys

data = os.environ.get("FLICKR_ORGANIZER_DATA", "/var/data")
user = pwd.getpwnam("app")
if os.getuid() == 0:
    os.makedirs(data, exist_ok=True)
    for root, dirs, files in os.walk(data):
        for path in [root, *(os.path.join(root, n) for n in dirs + files)]:
            try:
                os.lchown(path, user.pw_uid, user.pw_gid)
            except OSError:
                pass
    os.chmod(data, 0o700)
    os.setgroups([])
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
os.environ["HOME"] = user.pw_dir
os.execvp(sys.argv[1], sys.argv[1:])
