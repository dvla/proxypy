#!/bin/bash

function globals()
{
	echo "[globals]"
	echo "logfile = /var/log/proxypy.log"
	echo "zabbix = $1"
	echo ""
}

function host()
{
	echo "[node${1}]"
	echo "id = ${1}"
	echo "hostname = ${2}"
	echo "port = 8085"
	echo "unicast = 255.255.255.255:8086"
	echo "endpoint = ${3}"
	echo "threshold = 5"
	echo "backlog = 4"
	echo ""
}

case $1 in
"zabbix")	globals $2	;;
"host")		host $2 $3 $4	;;
*)		echo "invalid"	;;
esac

