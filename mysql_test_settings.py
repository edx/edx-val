import pymysql
pymysql.install_as_MySQLdb()
from edxval.settings.base import *
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.mysql', 'NAME': 'valtest',
    'USER': 'root', 'PASSWORD': '', 'HOST': '127.0.0.1', 'PORT': '3399',
}}
