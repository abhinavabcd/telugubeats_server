from gevent.greenlet import Greenlet
import time
import id3reader
import array
import os
import gevent
from buffers import Buffer
import config
from models.polls import Poll
from requests import stream_events_handler
from enums import Event
from responses.stream import InitData
from bson import json_util
from datetime import datetime
import urllib
from beta.seekable_file_block_url import RemoteUrlFileObject



class AudioStreamReader(Greenlet):
    stream_id = None
    fd = None
    id3 = None
    last_time_stamp  = time.time()
    stream_buffers = {
                      "telugu": [Buffer() , None]
    }
    #bit rate = (319/8)*1024 bytes/s
    #
    
    
    sleep_time = Buffer.CHUNK_BYTE_SIZE/((128.1/8)*1024) #128kbps
    # write to telugu buffer
    def __init__(self, stream_id):
        Greenlet.__init__(self)
        self.stream_id = stream_id
        self.buffer = AudioStreamReader.stream_buffers[self.stream_id][0]
    def _run(self):
        print "loading audio stream :: ", self.stream_id
        while(True):
            song_url_path = None
            current_poll = Poll.get_current_poll(self.stream_id)
            if(current_poll):
                song = current_poll.get_highest_poll_song(self.stream_id)
                if(song):
                    song_url_path = song.path
                    
            poll = Poll.create_next_poll(self.stream_id)
            
            if(not song_url_path):
                song_url_path = "http://storage.googleapis.com/telugubeats_files/music/Telugu/devisri%20prasad/arya/amalapuram.mp3"
                
            if(song_url_path.startswith("/Users/abhinav/Desktop/Telugu//")):                    
                song_url_path="http://storage.googleapis.com/telugubeats_files/music/Telugu/"+urllib.quote(song_url_path[31:])
                
            print "playing::", song_url_path
            self.fd = RemoteUrlFileObject(song_url_path)
            
            #spawn greenlet, keep reading into buffer
            #block calls to seek and read if buffer is not sufficient enough
            
            
            reset_data = InitData()
            
            reset_data.poll = poll
            reset_data.n_user = 1000+len( stream_events_handler.event_listeners[self.stream_id])
            reset_data.current_song  = song
            song.last_played = datetime.utcnow()
            song.save()
            event_data = json_util.dumps(reset_data.to_son())
            
            stream_events_handler.publish_event(self.stream_id, Event.RESET_POLLS_AND_SONG, event_data, from_user = None)

            # if there is valid ID3 data, read it out of the file first,
            # so we can skip sending it to the client
            try:
                self.id3 = id3reader.Reader(self.fd)
                if isinstance(self.id3.header.size, int): # read out the id3 data
                    self.fd.seek(self.id3.header.size)
                
                while(True):
                    try:
                        cur_time = time.time()
                        if(cur_time- self.last_time_stamp > AudioStreamReader.sleep_time):
                            self.last_time_stamp = cur_time
                            self.buffer.queue_chunk(self.fd.read(Buffer.CHUNK_BYTE_SIZE))
                            gevent.sleep(AudioStreamReader.sleep_time- time.time()+self.last_time_stamp)
                    except EOFError:
                        self.fd.close()
                        break        
            except Exception as e:
                    print e
                    

def handle_audio_stream(stream_id, socket):    
    for i in config.RESPONSE:
        socket.send(i)
    buffer = AudioStreamReader.stream_buffers[stream_id][0]
    last_sent_time = time.time() # 4 chunks once  #16*chunks per second #bitrate , 16kbytes per second =>
    current_index = max(0 , buffer.get_current_head()-4)
    
    while True:
        cur_time = time.time()
        if(cur_time - last_sent_time> AudioStreamReader.sleep_time):
            last_sent_time = cur_time
            chunk = buffer.get_chunk(current_index)
            #chunk = 32kb = > 16kbytes/sec => 1 chunks per 2 seconds 
            if(chunk):
                try:
                    n = 0
                    while(n<len(chunk)):    
                        n += socket.send(chunk[n:])
                    current_index+=1
                    gevent.sleep(AudioStreamReader.sleep_time - time.time()+last_sent_time)
                except:
                    #client disconnected
                    break
            else:
                gevent.sleep(AudioStreamReader.sleep_time)
        else:
            gevent.sleep(1)
        