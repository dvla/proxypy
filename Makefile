VERSION="1.2"

all:	configs scripts styles

configs:
	scp proxypy.ini root@micro1:/etc/ 
	scp proxypy.ini root@micro2:/etc/

scripts:
	scp proxy.py root@micro1:/root/proxy
	scp proxy.py root@micro2:/root/proxy
	
styles:
	scp style.css root@micro1:/var/www/
	scp style.css root@micro2:/var/www/

rpm:
	tap2rpm -y python -t proxy.py -m "HM Government (Driver and Vehicle Licensing Agency)" -e "Distributed Rate Limiting Proxy" --set-version=${VERSION}

