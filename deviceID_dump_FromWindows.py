#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 
# Author : AloneMonkey
# Editor : Kai_HT
# blog: www.alonemonkey.com
#
# The following programs are required to be stored in the location 'C:\Windows\System32'.
# http://stahlworks.com/dev/?tool=zipunzip

from __future__ import print_function
from __future__ import unicode_literals
import sys
import codecs
import frida
import threading
import os
import shutil
import time
import argparse
import tempfile
import subprocess
import re
import paramiko
from paramiko import SSHClient
from scp import SCPClient
from tqdm import tqdm
import traceback
import stat

# Add Import 
import platform
import time



def remove_readonly(func, path, _):
    """Windows 읽기 전용 속성 제거 핸들러"""
    os.chmod(path, stat.S_IWRITE)
    func(path)

IS_PY2 = sys.version_info[0] < 3
if IS_PY2:
    reload(sys)
    sys.setdefaultencoding('utf8')

script_dir = os.path.dirname(os.path.realpath(__file__))

DUMP_JS = os.path.join(script_dir, 'dump.js')

User = 'root'
Password = 'alpine'
Host = 'localhost'
Port = 22
KeyFileName = None

TEMP_DIR = os.path.join(os.path.expanduser('~'), 'Desktop', 'dump_temp')
PAYLOAD_DIR = 'Payload'
PAYLOAD_PATH = os.path.join(TEMP_DIR, PAYLOAD_DIR)
file_dict = {}

finished = threading.Event()

def get_usb_iphone(device_id=None):
    Type = 'usb'
    if int(frida.__version__.split('.')[0]) < 12:
        Type = 'tether'
    device_manager = frida.get_device_manager()
    changed = threading.Event()

    def on_changed():
        changed.set()

    device_manager.on('changed', on_changed)

    device = None
    while device is None:
        devices = [dev for dev in device_manager.enumerate_devices() 
                  if dev.type == Type and (not device_id or dev.id == device_id)]
        
        if len(devices) == 0:
            print(f'Waiting for USB device {device_id or ""}...')
            changed.wait()
        else:
            device = devices[0] if not device_id else next((d for d in devices if d.id == device_id), None)

    device_manager.off('changed', on_changed)
    return device

def generate_ipa(path, display_name):
    ipa_filename = display_name + '.ipa'
    print('Generating "{}"'.format(ipa_filename))
    try:
        app_name = file_dict['app']
        if platform.system() == 'Windows':
            for key in file_dict.keys():
                if key != 'app':
                    file_path = os.path.join(path, key)
                    subprocess.check_call(['icacls', file_path, '/grant', 'Administrators:F', '/T'], shell=True)
                    os.chmod(file_path, 0o777)
                    
        for key, value in file_dict.items():
            if key == 'app':
                continue
            from_dir = os.path.join(path, key)
            to_dir = os.path.join(path, app_name, value)
            shutil.move(from_dir, to_dir)

        target_dir = './' + PAYLOAD_DIR
        ipa_path = os.path.join(os.getcwd(), ipa_filename)
        zip_args = ('zip', '-qr', ipa_path, target_dir)
        subprocess.check_call(zip_args, cwd=TEMP_DIR)
        print('\n[SUCCESS] IPA 저장 경로:', os.path.abspath(ipa_path))
                
    except Exception as e:
        print(f"IPA 생성 실패: {e}")
    finally:
        if os.path.exists(PAYLOAD_PATH):
            try:
                # Windows 전용 강제 삭제
                if platform.system() == 'Windows':
                    subprocess.check_call(
                        ['takeown', '/F', PAYLOAD_PATH, '/R', '/D', 'Y'],
                        shell=True
                    )
                    subprocess.check_call(
                        ['icacls', PAYLOAD_PATH, '/grant', 'Administrators:F', '/T'],
                        shell=True
                    )
                shutil.rmtree(PAYLOAD_PATH, onerror=remove_readonly)
            except Exception as e:
                print(f"청소 실패: {e}")
        finished.set()


def on_message(message, data):
    t = tqdm(unit='B',unit_scale=True,unit_divisor=1024,miniters=1)
    last_sent = [0]

    def progress(filename, size, sent):
        baseName = os.path.basename(filename)
        if IS_PY2 or isinstance(baseName, bytes):
            t.desc = baseName.decode("utf-8")
        else:
            t.desc = baseName
        t.total = size
        t.update(sent - last_sent[0])
        last_sent[0] = 0 if size == sent else sent

    if 'payload' in message:
        payload = message['payload']
        if 'dump' in payload:
            origin_path = payload['path']
            dump_path = payload['dump']
            scp_from = dump_path
            scp_to = PAYLOAD_PATH + '/'

            with SCPClient(ssh.get_transport(), progress = progress, socket_timeout = 60) as scp:
                scp.get(scp_from, scp_to)

            chmod_dir = os.path.join(PAYLOAD_PATH, os.path.basename(dump_path))
            
            # Windows에서만 실행 방지
            if platform.system() != 'Windows':
                try:
                    os.chmod(chmod_dir, stat.S_IREAD | stat.S_IWRITE)
                except Exception as e:
                    print(f"권한 설정 실패: {e}")

            index = origin_path.find('.app/')
            file_dict[os.path.basename(dump_path)] = origin_path[index + 5:]

        if 'app' in payload:
            app_path = payload['app']
            scp_from = app_path
            scp_to = PAYLOAD_PATH + '/'
            with SCPClient(ssh.get_transport(), progress=progress, socket_timeout=120) as scp:
                scp.get(scp_from, scp_to, recursive=True)

            chmod_dir = os.path.join(PAYLOAD_PATH, os.path.basename(app_path))
            # Windows에서만 실행 방지
            if platform.system() != 'Windows':
                try:
                    os.chmod(chmod_dir, 0o755)
                except Exception as e:
                    print(f"권한 설정 실패: {e}")

            file_dict['app'] = os.path.basename(app_path)

        if 'done' in payload:
            finished.set()
    t.close()

def compare_applications(a, b):
    a_is_running = a.pid != 0
    b_is_running = b.pid != 0
    if a_is_running == b_is_running:
        if a.name > b.name:
            return 1
        elif a.name < b.name:
            return -1
        else:
            return 0
    elif a_is_running:
        return -1
    else:
        return 1


def cmp_to_key(mycmp):
    """Convert a cmp= function into a key= function"""

    class K:
        def __init__(self, obj):
            self.obj = obj

        def __lt__(self, other):
            return mycmp(self.obj, other.obj) < 0

        def __gt__(self, other):
            return mycmp(self.obj, other.obj) > 0

        def __eq__(self, other):
            return mycmp(self.obj, other.obj) == 0

        def __le__(self, other):
            return mycmp(self.obj, other.obj) <= 0

        def __ge__(self, other):
            return mycmp(self.obj, other.obj) >= 0

        def __ne__(self, other):
            return mycmp(self.obj, other.obj) != 0

    return K


def get_applications(device):
    try:
        applications = device.enumerate_applications()
    except Exception as e:
        sys.exit('Failed to enumerate applications: %s' % e)

    return applications


def list_applications(device):
    applications = get_applications(device)

    if len(applications) > 0:
        pid_column_width = max(map(lambda app: len('{}'.format(app.pid)), applications))
        name_column_width = max(map(lambda app: len(app.name), applications))
        identifier_column_width = max(map(lambda app: len(app.identifier), applications))
    else:
        pid_column_width = 0
        name_column_width = 0
        identifier_column_width = 0

    header_format = '%' + str(pid_column_width) + 's  ' + '%-' + str(name_column_width) + 's  ' + '%-' + str(
        identifier_column_width) + 's'
    print(header_format % ('PID', 'Name', 'Identifier'))
    print('%s  %s  %s' % (pid_column_width * '-', name_column_width * '-', identifier_column_width * '-'))
    line_format = '%' + str(pid_column_width) + 's  ' + '%-' + str(name_column_width) + 's  ' + '%-' + str(
        identifier_column_width) + 's'
    for application in sorted(applications, key=cmp_to_key(compare_applications)):
        if application.pid == 0:
            print(line_format % ('-', application.name, application.identifier))
        else:
            print(line_format % (application.pid, application.name, application.identifier))


def load_js_file(session, filename):
    source = ''
    with codecs.open(filename, 'r', 'utf-8') as f:
        source = source + f.read()
    script = session.create_script(source)
    script.on('message', on_message)
    script.load()

    return script

def create_dir(path):
    if os.path.exists(path):
        shutil.rmtree(path, onerror=remove_readonly)
    os.makedirs(path, exist_ok=True)

    if os.path.exists(path):
        shutil.rmtree(path, onerror=remove_readonly)
    os.makedirs(path, exist_ok=True)

def fix_windows_permissions(path):
    if platform.system() == 'Windows':
        for root, dirs, files in os.walk(path):
            for item in dirs + files:
                full_path = os.path.join(root, item)
                try:
                    subprocess.check_call(['attrib', '-R', full_path, '/S', '/D'])
                except subprocess.CalledProcessError as e:
                    print(f"속성 변경 실패: {e}")



def open_target_app(device, name_or_bundleid):
    print('Start the target app {}'.format(name_or_bundleid))

    apps = get_applications(device)
    found_apps = [
        app for app in apps 
        if name_or_bundleid in (app.identifier, app.name)
    ]
	
    if not found_apps:
        raise Exception(f"Failed Load Apps: {name_or_bundleid}")

    app = found_apps[0]
    pid = app.pid
    display_name = app.name
    bundle_identifier = app.identifier

    try:
        if not pid:
            print(f"앱 실행 시도: {bundle_identifier}")
            pid = device.spawn([bundle_identifier])
            session = device.attach(pid)
            device.resume(pid)
            time.sleep(2)  # 앱 실행 대기 추가
        else:
            session = device.attach(pid)
        return session, display_name, bundle_identifier
    except Exception as e:
        print(f"앱 실행 실패: {e}")
        raise

    pid = ''
    session = None
    display_name = ''
    bundle_identifier = ''

    for application in get_applications(device):
        if name_or_bundleid == application.identifier or name_or_bundleid == application.name:
            pid = application.pid
            display_name = application.name
            bundle_identifier = application.identifier

    try:
        if not pid:
            pid = device.spawn([bundle_identifier])
            session = device.attach(pid)
            device.resume(pid)
        else:
            session = device.attach(pid)
    except Exception as e:
        print(e) 

    return session, display_name, bundle_identifier


def start_dump(session, ipa_name):
    print('Dumping {} to {}'.format(display_name, TEMP_DIR))

    script = load_js_file(session, DUMP_JS)
    script.post('dump')
    finished.wait()

    generate_ipa(PAYLOAD_PATH, ipa_name)

    if session:
        session.detach()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='frida-ios-dump (by AloneMonkey v2.0), Edit By Kai_HT')
    parser.add_argument('-l', '--list', dest='list_applications', action='store_true', help='List the installed apps')
    parser.add_argument('-o', '--output', dest='output_ipa', help='Specify name of the decrypted IPA')
    parser.add_argument('-H', '--host', dest='ssh_host', help='Specify SSH hostname')
    parser.add_argument('-p', '--port', dest='ssh_port', help='Specify SSH port')
    parser.add_argument('-u', '--user', dest='ssh_user', help='Specify SSH username')
    parser.add_argument('-P', '--password', dest='ssh_password', help='Specify SSH password')
    parser.add_argument('-K', '--key_filename', dest='ssh_key_filename', help='Specify SSH private key file path')
    parser.add_argument('target', nargs='?', help='Bundle identifier or display name of the target app')
    parser.add_argument('-d', '--device-id', dest='device_id', help='Specify USB device ID to avoid conflicts')

    args = parser.parse_args()
    exit_code = 0
    ssh = None

    if not len(sys.argv[1:]):
        parser.print_help()
        sys.exit(exit_code)

    if args.list_applications:
        device = get_usb_iphone(args.device_id)
        list_applications(device)
    else:
        name_or_bundleid = args.target
        output_ipa = args.output_ipa

        # update ssh args
        if args.ssh_host:
            Host = args.ssh_host
        if args.ssh_port:
            Port = int(args.ssh_port)
        if args.ssh_user:
            User = args.ssh_user
        if args.ssh_password:
            Password = args.ssh_password
        if args.ssh_key_filename:
            KeyFileName = args.ssh_key_filename
        if not args.target:
            print("Error: Target app must be specified!")
            sys.exit(1)

        else:
            device = get_usb_iphone(args.device_id)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(Host, port=Port, username=User, password=Password, key_filename=KeyFileName)

            create_dir(PAYLOAD_PATH)
            (session, display_name, bundle_identifier) = open_target_app(device, name_or_bundleid)
            if output_ipa is None:
                output_ipa = display_name
            output_ipa = re.sub('\.ipa$', '', output_ipa)
            if session:
                start_dump(session, output_ipa)
        except paramiko.ssh_exception.NoValidConnectionsError as e:
            print(e)
            print('Try specifying -H/--hostname and/or -p/--port')
            exit_code = 1
        except paramiko.AuthenticationException as e:
            print(e)
            print('Try specifying -u/--username and/or -P/--password')
            exit_code = 1
        except Exception as e:
            print('*** Caught exception: %s: %s' % (e.__class__, e))
            traceback.print_exc()
            exit_code = 1



        finally:
            if os.path.exists(PAYLOAD_PATH):
                fix_windows_permissions(PAYLOAD_PATH)
                for _ in range(3):  # 최대 3회 재시도
                    try:
                        shutil.rmtree(PAYLOAD_PATH, onerror=remove_readonly)
                        break
                    except PermissionError:
                        time.sleep(1)
            if ssh:
                ssh.close()
        sys.exit(exit_code)
