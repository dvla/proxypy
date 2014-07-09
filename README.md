```html
 _ __  _ __ _____  ___   _ _ __  _   _ 
| '_ \| '__/ _ \ \/ / | | | '_ \| | | |
| |_) | | | (_) >  <| |_| | |_) | |_| |
| .__/|_|  \___/_/\_\\__, | .__/ \__, |
|_|                  |___/|_|    |___/ 
```
Proxypy is a distributed rate-limited proxy server written in Python. It implements basic discovery based clustering over UDP or Multicast and can operate against either HTTP or HTTPS targets. (the server itself is HTTP) 

Start in background with;
``` twistd -y proxypy.py
```
The config file on each machine needs to be identical. The code determines which settings to use by matching each machines's "hostname" against the hostname setting in a given stanza. Each node needs a unique ID, master nodes are chosen from available nodes with the lowest id taking priority.

```html
[node name]     # description only
id              # unique integer
hostname        # must match host this stanza applies to
unicast        	# signify UDP clustering
multicast       # signify multicast clustering
endpoint        # the gateway we're rate limiting
threshold       # max requests per second
backlog         # number of seconds to buffer on this machine
ssl             # use ssl endpoint (yes/no)
keyfile         # full path to SSL key file (client)
crtfile         # full path to SSL crt file (client)
```

Unicast example;

```html
[node2]
id              = 2
hostname        = micro2
port            = 8090
unicast         = 255.255.255.255:8070
endpoint        = localhost:80
threshold       = 5
backlog         = 4
ssl             =  yes
keyfile         = /etc/proxypy/client.key
crtfile         = /etc/proxypy/client.crt
```

Multicast equivalent;

```html
[node2]
id             = 2
hostname       = micro2
port           = 8090
multicast      = 228.0.0.9:8070
endpoint       = localhost:80
threshold      = 5
backlog        = 4
ssl            = no
```

The unicast / multicast line must be common to all gateways on the same throttle.  Unicast must be 255, multicast must be a multicast address.  To run multiple parallel throttles, group by unicast/multicast address.
                                       
```html
 _ __  _ __ _____  ___   _ _ __  _   _ 
| '_ \| '__/ _ \ \/ / | | | '_ \| | | |
| |_) | | | (_) >  <| |_| | |_) | |_| |
| .__/|_|  \___/_/\_\\__, | .__/ \__, |
|_|                  |___/|_|    |___/ 
