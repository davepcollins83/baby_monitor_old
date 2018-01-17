# A baby sleep monitor using a Raspberry Pi - cloned from https://github.com/srinathava/raspberry-pi-sleep-monitor

This setup shows how to create a baby sleep monitor which is able to stream a low latency image stream from a Raspberry Pi to a computer.

Browse the [Wiki page](https://github.com/srinathava/raspberry-pi-sleep-monitor/wiki) for instructions on setup and usage.

Main modifications:

Changed GStreamer to python, source to alsa for MEMS mic, use Gst events to get peak volume
Heavily modified index.html to be main page

