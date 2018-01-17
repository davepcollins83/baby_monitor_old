#!/bin/sh

# The following gstreamer pipeline(s) 
# 1. broadcast OPUS encoded audio to UDP port 5002 which is then converted
#    to a WebRTC stream by Janus 
# 2. broadcast raw JPEG frames to TCP port 9999. This is then read in by
#    the mpeg_server.py script and packaged into a multi-part stream so
#    that a browser can display it
# 
# A few subtle points in this pipeline which took some debugging to figure
# out:
# 1. tcpclient needs to have the host property set otherwise it tries to
# use a IPV6 instead of IPV4 port.
# 2. Need to use queue's after the tee branches otherwise the second branch
# of the tee "stalls" i.e., never seems to run.

gst-launch-1.0 -v \
    alsasrc  device="plug:dmic_sv" \
        ! audioresample \
        ! audio/x-raw,channels=2,layout="interleaved",rate=16000,format="S16LE" \
        ! opusenc bitrate=20000 \
        ! rtpopuspay \
        ! udpsink host=127.0.0.1 port=5002

#     ! ladspa-caps-so-noisegate open=-60.0 close=-80 attack=0 \
#        ! webrtcechoprobe \

#    	! audioamplify amplification=10 \

#device=plughw:1,0

# gst-launch-1.0 alsasrc device="plug:dmic_sv" ! audioconvert ! wavenc ! filesink location=test.wav