<PRE>proxypy
=======

Distributed Rate-Limited proxy.
Set configuration file in /etc and edit as appropriate.
Start in background with;
twistd -y proxy.py

Config file on each machine needs to be identical, appropriate config is
found by matching each machines's "hostname" against the hostname setting
in a given stanza.

Each node needs a unique ID, master nodes are chosen from available nodes with
the lowest id taking priority.

[node name]	# description only
id		# unique integer
hostname	# must match host this stanza applies to
unicast | multicast	# specify clustering protocol
endpoint	# the gateway we're rate limiting
threshold	# max requests per second
backlog		# number of seconds to buffer on this machine

Unicast example;

[node2]
id              = 2
hostname        = micro2
port            = 8090
unicast         = 255.255.255.255:8070
endpoint        = localhost:80
threshold       = 5
backlog         = 4

Multicast equivalent;

[node2]
id             = 2
hostname       = micro2
port           = 8090
multicast      = 228.0.0.9:8070
endpoint       = localhost:80
threshold      = 5
backlog        = 4

Note;

The unicast / multicast line must be common to all gateways on the same
throttle. Unicast must be 255, multicast must be a multicast address.

To run multiple parallel throttles, group by unicast/multicast address.

</PRE>

