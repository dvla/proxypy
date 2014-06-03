#!/usr/bin/python
##############################################################################
#                                                                           #
#   proxy.py                                                                #
#                                                                           #
#   Distributed Rate Limiting Proxy 					    #
#   Gareth Bult, 2014                                                       #
#                                                                           #
#############################################################################
#
#   TODO :: Maybe (?) make page an in-place update with JSON changeset?
#   TODO :: Add DOS mitigation
#   TODO :: Add Blacklisting
#   TODO :: Add Whitelisting
#
#############################################################################
#
#   Python Imports ...
#
import sys
import Queue
import syslog
#
from ConfigParser               import RawConfigParser as ConfParser
from time                       import time, sleep
from socket                     import gethostname
from urlparse                   import urlparse, urlunparse
from twisted.python             import log,logfile
from twisted.web                import http, proxy
from twisted.web.server         import NOT_DONE_YET
from twisted.internet           import reactor, threads, ssl
from twisted.application        import service, internet
from twisted.internet.protocol  import DatagramProtocol
from zbxsend                    import Metric, send_to_zabbix
from socket                     import SOL_SOCKET, SO_BROADCAST
#
#############################################################################
#                                                                           #
#   Load up defaults from the INI file (we insist on having the file!)      #
#                                                                           #
#############################################################################
#
INIFILE     = "/etc/proxypy.ini"
ini         = ConfParser()
try:
    arr = ini.read(INIFILE)
except IOError:
    print("Unable to read: ",INIFILE)
    exit(0)

def getOpt(section_name,option,default):
    """  wrapper for config parser """
    if not ini.has_option(section_name,option): return default
    return ini.get(section_name,option)

LOGFILE     = getOpt('globals','logfile','/tmp/proxy_py.log')
MULTICAST   = False

#############################################################################
#                                                                           #
#   Class :: Statistics                                                     #
#                                                                           #
#   We will use a global instance of this to track performance stats        #
#                                                                           #
#############################################################################

class Statistics():
    """ performance counters """
    total_queued = 0
    total_serviced = 0
    total_overload = 0
    avg_time_taken = 0

#############################################################################
#                                                                           #
#   Class :: PingPongClient                                                 #
#                                                                           #
#   This handled the inter-machine clustering                               #
#                                                                           #
#############################################################################

class PingPong(DatagramProtocol):
    """ object for telling the world we exist """

    def __init__(self,parent):
        """ set the output port # """
        self.finished       = False
        self.parent         = parent
        self.hostname       = gethostname()
        self.tokens         = Queue.Queue()
        self.hungry         = Queue.Queue()
        self.is_master      = False
        self.notifyz        = False
        self.peers          = {}
        self.handlers       = {}
        self.address        = (self.parent.mhost,self.parent.mport)
        #self.address        = ("255.255.255.255",self.parent.mport)

        self.handlers['get_token']   = self.get_token
        self.handlers['send_token']  = self.send_token
        self.handlers['pong']        = self.pong
        self.handlers['ping']        = self.ping
        # initialise the tokens for when we run as a master ...
        for i in xrange(0,self.parent.threshold): self.tokens.put_nowait(0)

    def stop(self):
        """ try to be neat and shut down the pingpong server cleanly """
        log.msg("=> [Stopping Stats]")
        self.finished = True

    def allocate_token(self):
        """ allocate a token and wait as necessary """
        token = self.tokens.get(True)
        stamp = time()
        diff  = ((token + 1) - stamp - 0.003)
        if diff > 0: sleep(diff)
        self.tokens.put_nowait(time())

    def getToken(self):
        """ recover a valid token from the master """
        self.transport.write("get_token:%d" % self.parent.id,self.address)

        try:
            self.hungry.get(True,1)
        except Queue.Empty:
            log.msg("** WARNING :: NO MASTER NODE IS ACIVE **")

    def startProtocol(self):
        """ come here once we enter listening mode """
        if MULTICAST:
            self.transport.setTTL(5)
            self.transport.joinGroup(self.parent.mhost)
        else:
            self.transport.socket.setsockopt(SOL_SOCKET, SO_BROADCAST, True)

    def run(self):
        """ this is our main proxy ping-pong loop """
        last_denied = 0

        while not self.finished:
            #
            #   Array of stats to send to Zabbix
            #
            denied = self.parent.statistics.total_overload - last_denied
            last_denied = self.parent.statistics.total_overload
            #
            metrics = []
            metrics.append(Metric(self.hostname,'limiter.current',len(self.parent.connection_map)))
            metrics.append(Metric(self.hostname,'limiter.queued',self.parent.request_queue.qsize()))
            metrics.append(Metric(self.hostname,'limiter.rejected',denied))

            self.transport.write("ping:%d" % self.parent.id,self.address)

            sleep(1)
            lowest = 99
            for peer in self.peers.keys():
                row = self.peers[peer]
                age = time()-row[0]
                if age>2: continue
                myid = int(row[3])
                if myid <= lowest: lowest = myid

            old_master = self.is_master
            self.is_master = self.parent.id == lowest
            if old_master <> self.is_master:
                if self.is_master:
                    log.msg("** [%d] Election :: We Won!" % self.parent.id)
                else:
                    log.msg("** [%d] Election :: We Lost!" % self.parent.id)

            monitor.send(metrics)

    def datagramReceived(self, datagram, address):
        """ handle an incoming packet """
        rec = datagram.split(":")
        cmd = rec[0]
        if cmd in self.handlers: self.handlers[cmd](rec,address)
        else: log.msg("Unknown datagram:"+datagram)

    def get_token(self,record,address):
        """ handle an incoming request for a token """
        if self.is_master:
            self.allocate_token()
            self.transport.write("send_token:%s" % record[1],address)

    def send_token(self,record,address):
        """ use the hungry queue to indicate token sent """
        if int(record[1]) == self.parent.id: self.hungry.put_nowait(0)

    def pong(self,record,address):
        """ record the date from an incoming pong message """
        self.peers[int(record[1])] = [time(),address] + record

    def ping(self,record,address):
        """ send out our statistics record  """
        s = self.parent.statistics
        qlen = self.parent.request_queue.qsize()
        self.transport.write("pong:%d:%d:%d:%d:%d:%.4f:%d" % (
            self.parent.id,qlen,s.total_queued,s.total_serviced,s.total_overload,s.avg_time_taken,self.is_master),address)

#############################################################################
#                                                                           #
#   Class :: ProxyClient                                                    #
#                                                                           #
#   Intercept to catch one specific error message - which we don't want     #
#                                                                           #
#############################################################################

class ProxyClient(proxy.ProxyClient):
    """ Only needed so we can overwrite the response handler (end) """
    def handleResponseEnd(self):
        """ It someone chopped the link before, don't finish() """
        if not self._finished:
            self._finished = True
            if not self.father._disconnected: self.father.finish()
            self.transport.loseConnection()

#############################################################################
#                                                                           #
#   Class :: ProxyClientFactory                                             #
#                                                                           #
#   This is an override to allow us to track connections                    #
#                                                                           #
#############################################################################

class ProxyClientFactory(proxy.ProxyClientFactory):
    """ intercept connection startup and shutdown """
    protocol = ProxyClient

    def startedConnecting(self,connector):
        """ intercept a connection start """
        connector._id = time()
        self.father.transport.server.factory.connection_map[connector._id] = connector
      
    def clientConnectionLost(self,connector,reason):
        """ intercept a connection stop """
        del self.father.transport.server.factory.connection_map[connector._id]

    def clientConnectionFailed(self,connector,reason):
        """ intercept a connection fail """
        del self.father.transport.server.factory.connection_map[connector._id]

#############################################################################
#                                                                           #
#   Class :: ProxyRequest                                                   #
#                                                                           #
#   This is an override to allow us to handle the transaction completion    #
#                                                                           #
#############################################################################

class ProxyRequest(proxy.ProxyRequest):
    """ this is where the transaction is initially received """
    protocols = dict(http=ProxyClientFactory)

    def do_stats(self,clientFactory):
        """ provide a user-friendly HTML response as an additional option """
        factory         = self.transport.server.factory
        request_queue   = factory.request_queue
        connection_map  = factory.connection_map
        say             = clientFactory.father.write
        req             = clientFactory.father
        headers         = clientFactory.headers

        req.setResponseCode(200,"OK")
        req.responseHeaders.addRawHeader("Content-Type","text/html")

        html  = "<html>"
        html += "<head>"
        html += "<link href='https://fonts.googleapis.com/css?family=Keania+One' rel='stylesheet' type='text/css'>"
        html += "<link href='https://fonts.googleapis.com/css?family=Nova+Square' rel='stylesheet' type='text/css'>"
        html += '<link type="text/css" href="/style.css" media="screen" rel="stylesheet">'
        #html += "<meta http-equiv='refresh' content='1'>"
        html += "</head>"
        html += "<body>"

        html += "<table><tr><td class='XXXX'>XXXX</td>"
        html += "<td class='LGG'>Legacy<br/>Gateway<br/>Guardian</td></tr></table>"

        html += "<h3>Cluster Status</h3><BR/>\n"
        html += "<table class='stats' cellspacing=2 cellpadding=4>\n"
        html += "<tr><th>Peer ID</th><th>Peer Address</th><th>Queue Size</th>"
        html += "<th>Queued</th><th>Serviced</th><th>Overload</th><th>Avg Time/Req</th>"
        html += "<th>Age</th></tr>\n"

        peers = self.transport.server.factory.ppc.peers
        for peer in peers.keys():
            when,addr,dummy,p,qlen,queued,serviced,overload,avg,master = peers[peer]
            age = time()-when
            tag = ''
            if age>1.5:
                tag = "class='peer_down'"
                mode = "Down"
            elif int(master):
                mode = "Master"
            else:
                mode = "Slave"

            html += "<tr %s><td class='peer'>%s - %s</td><td class='peer'>%s</td><td class='peer'>%s</td>" % (tag,peer,mode,addr[0],qlen)
            html += "<td class='peer'>%s</td><td class='peer'>%s</td><td class='peer'>%s</td><td class='peer'>%s</td><td class='peer'>%0.3f</td>" % (queued,serviced,overload,avg,time()-when)
            html += "</tr>\n"

        html += "<tr><td class='zabbix' colspan='8'>"
        html += monitor.status()
        html += "</td></tr>"
        html += "</table>\n"

        rep_host = "unknown"
        rep_ipv4 = "unknown"
        if headers.has_key('host'):      rep_host = headers['host']
        if headers.has_key('x-real-ip'): rep_ipv4 = headers['x-real-ip']
        html += "<p>Load balancer is [%s] Real client IP is [%s]</p><br/>" % (rep_host,rep_ipv4)

        html += "<h3>Endpoint Connections</h3><BR/>\n"
        html += "<table class='stats' cellspacing=2 cellpadding=4>\n"
        html += "<tr><th>Connection ID</th><th>Target Host</th><th>Target Port</th><th>State</th><th>Destination</th></tr>\n"
        say(html)
        html = ""
        
        for key in connection_map:
            c = connection_map[key]
            html += "<tr><td>%f</td><td>%s</td><td>%d</td><td>%s</td><td>%s</td></tr>\n" % (c._id,c.host,c.port,c.state,c.getDestination())

        html += "</table><br/>\n"

        html += "<h3>Outstanding Queue</h3><BR/>\n"
        html += "<table class='stats' cellspacing=2 cellpadding=4>\n"
        html += "<tr><th>Index</th><th>Client</th><th>Target</th></tr>\n"

        say(html)
        html = ""

        indx = 0
        for entry in request_queue.queue:
            client = "%s:%s" % (entry.father.host.host,entry.father.host.port)
            html += "<tr><td style:'width:100'>%d</td><td>%s</td><td>%s%s</td></tr>\n" % (indx,client,entry.headers['host'],entry.father.uri)
            indx += 1

        html += "</table>"
        html += "</body></html>"

        say(html)
        req.finish()

    def do_overload(self,xlist,clientFactory):
        """ this is what happens if we receive a request and the queue is full """
        req = clientFactory.father
        req.setResponseCode(413,"Service Overload")
        req.responseHeaders.addRawHeader("Content-Type","application/json")
        req.write("{'overLimit' : { 'code':413 , 'message':'OverLimit Retry ...' }")
        req.finish()
    
    def process(self):
        """ the is the request processor / main decision maker """
        factory = self.transport.server.factory
        parsed  = urlparse(self.uri)
        rest    = urlunparse(('', '') + parsed[2:])
        class_  = self.protocols['http']
        headers = self.getAllHeaders().copy()
        self.content.seek(0, 0)
        s = self.content.read()
        if not rest: rest += '/'
        clientFactory = class_(self.method, rest, self.clientproto, headers,s, self)

        if headers.has_key('x-real-ip'):
            ip = headers['x-real-ip']
        else:
            ip = self.client.host

        log.msg("client=%s method=%s uri=%s port=%s" %(ip,self.method,rest,self.host.port))

        request_queue = self.transport.server.factory.request_queue
        statistics    = self.transport.server.factory.statistics

        if      rest == "/stats": self.do_stats(clientFactory)
        else:
            if request_queue.qsize() >= (factory.threshold*factory.backlog):
                statistics.total_overload += 1
                self.do_overload(request_queue,clientFactory)
            else:
                statistics.total_queued += 1
                clientFactory.Qtime = time()
                request_queue.put(clientFactory,False)
                return NOT_DONE_YET

#############################################################################
#                                                                           #
#   Class :: Proxy                                                          #
#                                                                           #
#   Simple override to link "ProxyRequest" into the execution chain         #
#                                                                           #
#############################################################################

class Proxy(proxy.Proxy):
    """ set the request factory """
    requestFactory = ProxyRequest

#############################################################################
#                                                                           #
#   Class :: ProcessRequests                                                #
#                                                                           #
#   This runs in it's own thread and de-queue's requests from the backlog   #
#   at the throttled rate and submits them to the end-point                 #
#                                                                           #
#############################################################################

class ProcessRequests():
    """ de-queue thread, runs in twisted reactor """
    def __init__(self,parent):
        """ set up a local reference to the request queue """
        self.parent = parent
        self.finished = False

    def stop(self):
        """ initiate a stop """
        log.msg("=> [Flushing Queue]")
        self.finished = True

    def wait(self):
        """ make sure the queue is empty before we quit """
        while self.parent.request_queue.qsize(): sleep(1)
        log.msg("=> [Flushed]")

    def run(self):
        """ main loop - pretty simple """
        while True:
            try:
                clientFactory = self.parent.request_queue.get(True,1)
                if type(clientFactory) == type("Quit"): return
            except Queue.Empty:
                if self.finished: return
                continue

            self.parent.ppc.getToken()
            if self.parent.ssl:

                with open(self.parent.keyfile) as keyFile:
                    with open(self.parent.crtfile) as certFile:
                        clientCert = ssl.PrivateCertificate.loadPEM(
                        keyFile.read() + certFile.read())

                ctx = clientCert.options()
                #contextFactory = ssl.ClientContextFactory()
                reactor.connectSSL(self.parent.host, self.parent.port, clientFactory, ctx)
            else:
                reactor.connectTCP(self.parent.host, self.parent.port, clientFactory)

            time_taken = time()-clientFactory.Qtime
            stats = self.parent.statistics
            stats.avg_time_taken = (
                stats.total_serviced * stats.avg_time_taken + time_taken) \
                / (stats.total_serviced+1)
            stats.total_serviced += 1

#############################################################################
#                                                                           #
#   Class :: ProxyFactory                                                   #
#                                                                           #
#   Simple override to link "Proxy" into the execution chain                #
#                                                                           #
#############################################################################

class ProxyFactory(http.HTTPFactory):
    """ set the protocol handler """
    protocol         = Proxy

    def Ok(self):
        return self.ok

    def __init__(self,section_name):
        """ set up default values for the proxy server """
        http.HTTPFactory.__init__(self)
        self.ok = False
        self.id = int(getOpt(section_name,'id',0))
        if not self.id:
            log.msg("** Node ignored - no ID configured!")
            return
        try:
            self.host,port = getOpt(section_name,'endpoint','').split(":")
            self.port = int(port)
        except:
            log.msg("** 'endpoint' must be of the format: <address>:<port>")
            return

        self.keyfile = getOpt(section_name,'keyfile','')
        io = open(self.keyfile,'r')
        io.close()


        try:
            ssl = getOpt(section_name,'ssl','no')
            self.ssl = not (ssl == 'no')
            if self.ssl:
                try:
                    self.keyfile = getOpt(section_name,'keyfile','')
                    io = open(self.keyfile,'r')
                    io.close()
                except:
                    log.msg("** missing keyfile='' or can't open keyfile")
                    return
                try:
                    self.crtfile = getOpt(section_name,'crtfile','')
                    io = open(self.crtfile,'r')
                    io.close()
                except:
                    log.msg("** missing crtfile='' or can't open crtfile ")
                    return

        except:
            log.msg("** 'ssl' must be 'yes' or 'no")
            return

        if ini.has_option(section_name,'multicast'):
            try:
                self.mhost,port = getOpt(section_name,'multicast','').split(":")
                self.mport = int(port)
            except:
                log.msg("** 'multicast' must be of the format: <address>:<port>")
                return
        else:
            try:
                self.mhost,port = getOpt(section_name,'unicast','').split(":")
                self.mport = int(port)

            except:
                log.msg("** 'unicast' must be of the format: <address>:<port>")
                return

        self.threshold        = int(getOpt(section_name,'threshold','5'))
        self.backlog          = int(getOpt(section_name,'backlog','5'))
        self.statistics       = Statistics()
        self.request_queue    = Queue.Queue()
        self.connection_map   = {}
        self.ppc              = PingPong(self)
        self.process_requests = ProcessRequests(self)
        self.ok               = True

    def startFactory(self):
        """ kick off the queue processor """
        if MULTICAST:
            log.msg("Multicast Listening on [%s] [%d]" % (self.mhost,self.mport))
            reactor.listenMulticast(int(self.mport),self.ppc,listenMultiple=True)
        else:
            log.msg("UDP Listening on [%d]" % (self.mport))
            reactor.listenUDP(int(self.mport),self.ppc)

        threads.deferToThread(self.ppc.run)
        threads.deferToThread(self.process_requests.run)
        reactor.addSystemEventTrigger('during','shutdown',self.ppc.stop)
        reactor.addSystemEventTrigger('during','shutdown',self.process_requests.stop)
        reactor.addSystemEventTrigger('after','shutdown',self.process_requests.wait)

    def stopFactory(self):
        """ simple tweak to send a message to the de-queuer when we exit """
        self.request_queue.put("Quit",False)
        monitor.quit()

class Monitor():
    """ monitor object to queue logging for Zabbix """
    def __init__(self,server):
        """ set up the monitor """
        self.server   = server
        self.queue    = Queue.Queue()
        self.notifyz  = True
        self.Finished = False

    def status(self):
        """ generate some status HTML for the /status handler """
        if MULTICAST:
            html = "  <span class='ok'><< Multicast >></span> "
        else:
            html = " <span class='ok'><< UDP >> </span>"
        if self.queue.qsize() < 3: state='ok'
        else: state='help'
        html += "Zabbix Server [<span class='%s'>%s</span>] " % (state,self.server)
        html += "Queue Size [<span class='%s'>%d</span>] " % (state,self.queue.qsize())
        html += "Server up = <span class='%s'>%s</span>" % (state,self.notifyz)
        return html

    def send(self,metrics):
        """ put an entry on the queue (so long as it's not full! """
        if self.queue.qsize() < 8192:
            self.queue.put_nowait(metrics)
        else:
            log.msg("Dropped Zabbix entry")

    def quit(self):
        """ tell the process it's done! """
        log.msg("Signal Zabbix Quit")
        self.Finished = True

    def run(self):
        """ main process loop, dequeue and send """
        while not self.Finished:
            try:
                metrics = self.queue.get(True,1)
            except Queue.Empty:
                continue
            while not self.Finished:
                status = send_to_zabbix(metrics,self.server,10051)
                if status: break
                sleep(2)
                log.msg("Zabbix Connection is currently down!")

        log.msg("=> Zabbix thread exit")

    def up(self):
        """ """
        if self.notifyz: log.msg("Zabbix Connection has come up!")
        self.notifyz = True

    def down(self):
        """ """
        if self.notifyz:
            log.msg("Zabbix Connection is currently down!")
            self.notifyz = False
        sleep(1)

#############################################################################
#                                                                           #
#   Main Section                                                            #
#                                                                           #
#############################################################################
#
#   If we run from the command line, run in the foreground
#
debug = __name__ == '__main__'
reactor.suggestThreadPoolSize(30)
#
#   And log everything to stdout
#
#if debug:
log.startLogging(sys.stdout)
#else:
    #log.startLogging(logfile.LogFile(LOGFILE,".",rotateLength=1024*1024*10))
if not debug:
    application = service.Application("Proxy Server")

monitor = Monitor(getOpt('globals','zabbix' ,'127.0.0.1'))

for section in ini.sections():
    if section == "globals": continue
    if getOpt(section,'hostname','') == gethostname():
        factory = ProxyFactory(section)
        if not factory.Ok(): continue
        port = int(getOpt(section,'port','80'))
        if debug:
            reactor.listenTCP(port,factory)
        else:
            service = internet.TCPServer(port,factory)
            service.setServiceParent(application)

threads.deferToThread(monitor.run)
if debug: reactor.run()
