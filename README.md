# ping-monitor

## Description

This is a super-simple ping monitor for MacOS that will continuously ping a
landmark host - typically 8.8.8.8 - and record downtimes.  
It also monitors a fixed interface name - typically en0 - and records outages
only if that interface is up. this is to account for times where the wifi
interface has been turned down voluntarily.

Without verbose mode, it will only print out the time and duration of each outage
