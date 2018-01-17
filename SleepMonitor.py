#!/usr/bin/env python

from twisted.internet import reactor, protocol, defer, interfaces
import twisted.internet.error
from twisted.web import server, resource
from twisted.web.static import File
from zope.interface import implementer

import jinja2

import pyglet

from random import *

import io
import re
from datetime import datetime, timedelta
import glob
import os
import json
import subprocess
import time

import Image
import ImageOps
import ImageFilter
import ImageChops

from MotionStateMachine import MotionStateMachine
from ProcessProtocolUtils import TerminalEchoProcessProtocol, \
        spawnNonDaemonProcess
from OximeterReader import OximeterReader

from LoggingUtils import *

from Config import Config
from Constants import *

template_dir = '{}/web/'.format(os.path.dirname(os.path.realpath(__file__)))

player = pyglet.media.Player()

os.system('modprobe w1-gpio')
os.system('modprobe w1-therm')
 
base_dir = '/sys/bus/w1/devices/'
device_folder = glob.glob(base_dir + '28*')[0]
device_file = device_folder + '/w1_slave'

def read_temp_raw():
    f = open(device_file, 'r')
    lines = f.readlines()
    f.close()
    return lines
 
def read_temp():
    lines = read_temp_raw()
    while lines[0].strip()[-3:] != 'YES':
        time.sleep(0.2)
        lines = read_temp_raw()
    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        temp_string = lines[1][equals_pos+2:]
        temp_c = float(temp_string) / 1000.0
        # temp_f = temp_c * 9.0 / 5.0 + 32.0
        return temp_c

def render(template_file, context):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    return env.get_template(template_file).render(context).encode('utf-8')

def async_sleep(seconds):
     d = defer.Deferred()
     reactor.callLater(seconds, d.callback, seconds)
     return d

class MJpegResource(resource.Resource):
    def __init__(self, queues):
        self.queues = queues

    def setupProducer(self, request):
        producer = JpegProducer(request)
        request.notifyFinish().addErrback(self._responseFailed, producer)
        request.registerProducer(producer, True)

        self.queues.append(producer)

    def _responseFailed(self, err, producer):
        log('connection to client lost')
        producer.stopProducing()

    def render_GET(self, request):
        log('getting new client of image stream')
        request.setHeader("content-type", 'multipart/x-mixed-replace; boundary=--spionisto')

        self.setupProducer(request)
        return server.NOT_DONE_YET

@implementer(interfaces.IPushProducer)
class JpegProducer(object):
    def __init__(self, request):
        self.request = request
        self.isPaused = False
        self.isStopped = False
        self.delayedCall = None

    def cancelCall(self):
        if self.delayedCall:
            self.delayedCall.cancel()
            self.delayedCall = None

    def pauseProducing(self):
        self.isPaused = True
        self.cancelCall()
        # log('producer is requesting to be paused')

    def resetPausedFlag(self):
        self.isPaused = False
        self.delayedCall = None

    def resumeProducing(self):
        # calling self.cancelCall is defensive. We should not really get
        # called with multiple resumeProducing calls without any
        # pauseProducing in the middle.
        self.cancelCall()
        self.delayedCall = reactor.callLater(1, self.resetPausedFlag)
        # log('producer is requesting to be resumed')

    def stopProducing(self):
        self.isPaused = True
        self.isStopped = True
        log('producer is requesting to be stopped')

MJPEG_SEP = '--spionisto\r\n'

class JpegStreamReader(protocol.Protocol):
    def __init__(self):
        self.tnow = None

    def connectionMade(self):
        log('MJPEG Image stream received')
        self.data = ''
        self.tnow = datetime.now()
        self.cumDataLen = 0
        self.cumCalls = 0

    def dataReceived(self, data):
        self.data += data

        chunks = self.data.rsplit(MJPEG_SEP, 1)

        dataToSend = ''
        if len(chunks) == 2:
            dataToSend = chunks[0] + MJPEG_SEP

        self.data = chunks[-1]

        self.cumDataLen += len(dataToSend)
        self.cumCalls += 1

        for producer in self.factory.queues:
            if (not producer.isPaused):
                producer.request.write(dataToSend)

        if datetime.now() - self.tnow > timedelta(seconds=1):
            # log('Wrote %d bytes in the last second (%d cals)' % (self.cumDataLen, self.cumCalls))
            self.tnow = datetime.now()
            self.cumDataLen = 0
            self.cumCalls = 0

class MotionDetectionStatusReaderProtocol(protocol.ProcessProtocol):
    PAT_STATUS = re.compile(r'(\d) (\d)')
    def __init__(self, app):
        self.data = ''
        self.motionDetected = False
        self.motionSustained = False
        self.app = app
        self.started = False

    def detectStartup(self, lines):
        for line in lines:
            if line.startswith('MOTION_DETECTOR_READY'):
                self.app.startGstreamerVideo()
                self.started = True

    def outReceived(self, data):
        self.data += data

        if self.data.startswith('MOTION_DETECTOR_READY'):
            self.app.startGstreamerVideo()

        lines = self.data.split('\n')
        if not self.started:
            self.detectStartup(lines)

        if len(lines) > 1:
            line = lines[-2]
            if self.PAT_STATUS.match(line):
                (self.motionDetected, self.motionSustained) = [int(word) for word in line.split()]

        self.data = lines[-1]

    def reset(self):
        self.transport.write('reset\n')

class StatusResource(resource.Resource):
    def __init__(self, app):
        self.app = app
        self.motionDetectorStatusReader = self.app.motionDetectorStatusReader
        self.oximeterReader = self.app.oximeterReader

    def render_GET(self, request):
        request.setHeader("content-type", 'application/json')

        motion = 0
        motionReason = MotionReason.NONE
        if self.motionDetectorStatusReader.motionSustained:
            motion = 1
            motionReason = MotionReason.CAMERA
        elif self.oximeterReader.motionSustained:
            motion = 1
            motionReason = MotionReason.BPM

        status = {
                'SPO2': self.oximeterReader.SPO2,
                'BPM': self.oximeterReader.BPM,
                'alarm': bool(self.oximeterReader.alarm),
                'motion': motion,
                'motionReason': motionReason,
                'readTime': self.oximeterReader.readTime.isoformat(),
                'oximeterStatus': self.oximeterReader.status
                }
        return json.dumps(status)

class PingResource(resource.Resource):
    def render_GET(self, request):
        request.setHeader("content-type", 'application/json')
        request.setHeader("Access-Control-Allow-Origin", '*')

        status = { 'status': 'ready'}
        return json.dumps(status)

class GetConfigResource(resource.Resource):
    def __init__(self, app):
        self.app = app

    def render_GET(self, request):
        request.setHeader("content-type", 'application/json')

        status = {}
        for paramName in self.app.config.paramNames:
            status[paramName] = getattr(self.app.config, paramName)

        return json.dumps(status)

class UpdateConfigResource(resource.Resource):
    def __init__(self, app):
        self.app = app

    def render_GET(self, request):
        log('Got request to change parameters to %s' % request.args)

        for paramName in self.app.config.paramNames:
            # a bit of defensive coding. We really should not be getting
            # some random data here.
            if paramName in request.args:
                paramVal = int(request.args[paramName][0])
                log('setting %s to %d' % (paramName, paramVal))
                setattr(self.app.config, paramName, paramVal)

        self.app.resetAfterConfigUpdate()

        request.setHeader("content-type", 'application/json')
        status = { 'status': 'done'}
        return json.dumps(status)

class Logger:
    def __init__(self, app):
        self.oximeterReader = app.oximeterReader
        self.motionDetectorStatusReader = app.motionDetectorStatusReader

        self.lastLogTime = datetime.min
        self.logFile = None

        reactor.addSystemEventTrigger('before', 'shutdown', self.closeLastLogFile)

    @defer.inlineCallbacks
    def run(self):
        while True:
            yield async_sleep(1)

            tnow = datetime.now()
            if self.oximeterReader.SPO2 != -1:
                tstr = tnow.strftime('%Y-%m-%d-%H-%M-%S')
                spo2 = self.oximeterReader.SPO2
                bpm = self.oximeterReader.BPM
                alarm = self.oximeterReader.alarm
                motionDetected = self.motionDetectorStatusReader.motionDetected
                motionSustained = self.motionDetectorStatusReader.motionSustained

                logStr = '%(spo2)d %(bpm)d %(alarm)d %(motionDetected)d %(motionSustained)d' % locals()

                log('STATUS: %s' % logStr)

                if self.logFile is None:
                    self.createNewLogFile(tstr)

                self.printToFile('%(tstr)s %(logStr)s' % locals())
                self.lastLogTime = tnow
            else:
                if tnow - self.lastLogTime > timedelta(hours=2):
                    self.closeLastLogFile()

    def closeLastLogFile(self):
        if self.logFile is not None:
            self.logFile.close()
            newname = self.logFile.name.replace('.inprogress', '')
            os.rename(self.logFile.name, newname)
            self.logFile = None

    def createNewLogFile(self, tstr):
        bufsize = 1 # line buffering

        if not os.path.isdir('../sleep_logs'):
            os.mkdir('../sleep_logs')

        self.logFile = open('../sleep_logs/%s.log.inprogress' % tstr, 'w', bufsize)

    def printToFile(self, logStr):
        self.logFile.write(logStr + '\n')

def startAudio():
    spawnNonDaemonProcess(reactor, TerminalEchoProcessProtocol(), '/opt/janus/bin/janus', 
                          ['janus', '-F', '/opt/janus/etc/janus/'])
    log('Started Janus')

    def startGstreamerAudio():
    	
    	spawnNonDaemonProcess(reactor, TerminalEchoProcessProtocol(), '/usr/bin/python', ['python', 'gstream_audio.py'])
        #spawnNonDaemonProcess(reactor, TerminalEchoProcessProtocol(), '/bin/sh', ['sh', 'gstream_audio_mod.sh'])
    	
        log('Started gstreamer audio')

    reactor.callLater(2, startGstreamerAudio)

def audioAvailable():
    out = subprocess.check_output(['arecord', '-l'])
    return ('USB Audio' in out)

def startAudioIfAvailable():
    if audioAvailable():
        startAudio()
    else:
        log('Audio not detected. Starting in silent mode')

class GetChart(resource.Resource):
	def __init__(self, app):
		self.app = app
		
	def render_GET(self, request):
		#request.setHeader("content-type", 'application/json')
		legend = 'Monthly Data'
		labels = ["January", "February", "March", "April", "May", "June", "July", "August"]
		
		values = [10, 9, 8, 7, 6, 4, 7, 8]
		data = {'legend': legend, 'labels': labels, 'values': values}
		return render('vartest.html', data)

class GetTemp(resource.Resource):
	def __init__(self, app):
		self.app = app
		
	def render_GET(self, request):
		request.setHeader("content-type", "text/html")
		temp = read_temp()
	
		return bytes(temp)
		
class PlayMusic(resource.Resource):
	def __init__(self, app):
		self.app = app
		
	def render_GET(self, request):
		
		global player
		
		player.queue(pyglet.media.load('media/bell.wav'))
		player.play()
		
		return	

class SleepMonitorApp:
    def startGstreamerVideo(self):

        videosrc = '/dev/video0'

        try:
            out = subprocess.check_output(['v4l2-ctl', '--list-devices'])
        except subprocess.CalledProcessError as e:
            out = e.output

        lines = out.splitlines()
        for (idx, line) in enumerate(lines):
            if 'bcm2835' in line:
                nextline = lines[idx+1]
                videosrc = nextline.strip()

        spawnNonDaemonProcess(reactor, TerminalEchoProcessProtocol(), '/bin/sh', 
                              ['sh', 'gstream_video.sh', videosrc])

        log('Started gstreamer video using device %s' % videosrc)
    
    def __init__(self):
        queues = []

        self.config = Config()
        self.reactor = reactor

        self.oximeterReader = OximeterReader(self)

        self.motionDetectorStatusReader = MotionDetectionStatusReaderProtocol(self)
        spawnNonDaemonProcess(reactor, self.motionDetectorStatusReader, 'python', 
                ['python', 'MotionDetectionServer.py'])
        log('Started motion detection process')

        logger = Logger(self)
        logger.run()
        log('Started logging')

        factory = protocol.Factory()
        factory.protocol = JpegStreamReader
        factory.queues = queues
        reactor.listenTCP(9999, factory)
        log('Started listening for MJPEG stream')

        root = File('web')
        root.putChild('stream.mjpeg', MJpegResource(queues))
        root.putChild('status', StatusResource(self))
        root.putChild('ping', PingResource())
        root.putChild('getConfig', GetConfigResource(self))
        root.putChild('updateConfig', UpdateConfigResource(self))
        root.putChild('getChart', GetChart(self))
        root.putChild('getTemp', GetTemp(self))
        root.putChild('playMusic', PlayMusic(self))

        site = server.Site(root)
        PORT = 80
        BACKUP_PORT = 8080
        try:
            reactor.listenTCP(PORT, site)
            log('Started webserver at port %d' % PORT)
        except twisted.internet.error.CannotListenError, ex:
            reactor.listenTCP(BACKUP_PORT, site)
            log('Started webserver at port %d' % BACKUP_PORT)

        startAudioIfAvailable()

        reactor.run()

    def resetAfterConfigUpdate(self):
        log('Updated config')
        self.config.write()
        self.oximeterReader.reset()
        self.motionDetectorStatusReader.reset()

if __name__ == "__main__":
    setupLogging()
    log('Starting main method of sleep monitor')
    try:
        app = SleepMonitorApp()
        pyglet.app.run()
    except:
        logging.exception("main() threw exception")
