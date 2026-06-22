from setuptools import setup

APP = ['app.py']
OPTIONS = {
    'argv_emulation': False,
    'packages': ['PIL', 'psd_tools', 'tkinter'],
    'iconfile': None,
    'plist': {
        'CFBundleName': 'idgen',
        'CFBundleDisplayName': 'idgen',
        'CFBundleIdentifier': 'com.cameroncohen.idgen',
        'CFBundleVersion': '1.0',
        'CFBundleShortVersionString': '1.0',
        'NSPrincipalClass': 'NSApplication',
    },
}

setup(
    name='idgen',
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
