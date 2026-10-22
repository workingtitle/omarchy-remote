"""A/V signaling identity; never return SDP or peer identifiers in telemetry."""
STREAM_ID='omarchy-media-v1'
def sdp_sync_flags(sdp):
    if not isinstance(sdp,str) or len(sdp)>131072:raise ValueError('Invalid SDP size')
    tracks={};current=None
    for line in sdp.splitlines():
        if line.startswith('m='):
            kind=line[2:].split(' ',1)[0];current={'msid':set(),'cname':set()} if kind in ('audio','video') else None
            if current is not None:tracks[kind]=current
        elif current is not None:
            if line.startswith('a=msid:'):
                words=line[7:].split()
                if words:current['msid'].add(words[0])
            elif line.startswith('a=ssrc:'):
                parts=line.split(' ',1)
                if len(parts)==2:
                    key,sep,value=parts[1].partition(':')
                    if sep and key in current and value:current[key].add(value.split()[0])
    a=tracks.get('audio',{});v=tracks.get('video',{})
    return {('shared_'+key):bool(a.get(key)) and a.get(key)==v.get(key) and len(a[key])==1 for key in ('msid','cname')}

def configure_sync(peer):
    pads=list(peer.sinkpads)
    if len(pads)!=2 or any(p.find_property('msid') is None for p in pads):
        raise RuntimeError('Shared media stream identity unavailable')
    for pad in pads:pad.set_property('msid',STREAM_ID)
    rtp=peer.get_by_name('rtpbin')
    if rtp is None or any(rtp.find_property(p) is None for p in ('ntp-time-source','rtcp-sync-send-time')):
        raise RuntimeError('Shared RTCP clock configuration unavailable')
    rtp.set_property('ntp-time-source',0) # common NTP clock, not separate source clocks
    rtp.set_property('rtcp-sync-send-time',False) # include pre-send encoder delay in synchronization
    return {'ntp_time_source':int(rtp.get_property('ntp-time-source')),
            'rtcp_sync_send_time':bool(rtp.get_property('rtcp-sync-send-time'))}
