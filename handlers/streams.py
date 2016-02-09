from gevent.greenlet import Greenlet
import time
import id3reader
import array
import os
import gevent
from gevent.queue import Queue
from buffers import Buffer
import config
from models.polls import Poll

from enums import Event
from bson import json_util
from datetime import datetime
import urllib
from beta.seekable_file_block_url import RemoteUrlFileObject
from config import IS_TEST_BUILD
from models.song import Song
import random
from mongoengine.document import Document
from mongoengine.fields import IntField, BooleanField, ListField, StringField,\
    DateTimeField
from models.events import StreamEvent
from logger import logger
from helpers.io_utils import response_write


streams = {}



def audio_publishing_thread(func):
    def wrapper(stream, *args):
        stream.audio_publishing_threads.append(Greenlet.spawn(func, stream , *args))
    return wrapper


def audio_source_reader(func):
    def wrapper(stream, *args):
        stream.audio_reader_thread= Greenlet.spawn(func, stream , *args)
    return wrapper



#has audio stream , has events stream and some numbers associated with it
class Stream(Document):
    #static data
    stream_id = StringField(unique=True)
    likes = IntField()
    subscribers = IntField()
    is_live = BooleanField()
    # to listen or broadcast to
    hosts = ListField(StringField())
    description = StringField()
    is_special_song_stream = BooleanField()
    # host from where we read the data into buffer and keep broadcasting
    source_host = StringField()
    is_scheduled = DateTimeField()
    title = StringField()
    image = StringField()
    additional_info = StringField()
    
    
    
    
    #live data
    event_listeners = {} # sockets list
    event_queue = None
    last_few_events = []
    stream_buffers_info = []

    event_publisher_thread = None  # this is a listener model
    
    events_reader_thread = None
    
    audio_reader_thread = None    
    audio_publishing_threads = [] # one for each clients
    
    
    
    
    def initialize(self):
        logger.debug("initializing stream " + self.stream_id)
        streams[self.stream_id] = self
                
        self.event_queue = Queue()
        self.last_few_events  = StreamEvent.get_events(self.stream_id)
                
        #start publishing to subscribers      
        self.event_publisher_thread = Greenlet.spawn(Stream.start_publishing_events, self)

        self.init_buffers()
        
        if(config.host_id != self.source_host): # is not the source , so keep reading from source host
            self.audio_reader_thread = Greenlet.spawn(Stream.start_reading_audio_from_source, self)
#TODO:            self.strat_reading_events_from_source()            

        else:# this is the source host
            if(self.is_special_song_stream):
                self.audio_reader_thread = Greenlet.spawn(Stream.start_reading_from_files, self)
                
            else:
                '''some user should be sending data to this host # nothing to do'''
                
                

    def init_buffers(self,bit_rate_in_kbps = 128.0):
        self.buffer = buffer =  Buffer()
        self.byte_rate = byte_rate = ((bit_rate_in_kbps/8)*1024)
        self.sleep_time = sleep_time = (buffer.chunk_byte_size*1.0)/byte_rate
        self.stream_buffers_info = [buffer , byte_rate, sleep_time]
        
    
    def start_reading_from_source(self):        
        if(self.is_special_song_stream):
            self.read_into_buffer_from_db_files()
    
    def start_reading_from_files(self):
        logger.debug("start reading from files " + self.stream_id)
                
        max_track_count = Song.objects().order_by("-track_n")[0].track_n
        logger.debug("max_tracks in db "+str(max_track_count))
        logger.debug("start reading audio stream :: " + self.stream_id)
        
        last_sent_time_stamp = 0 # last timestamp where we sent a packet
        while(True):
            song_url_path = None
            current_poll = Poll.get_current_poll(self.stream_id)
            if(current_poll):
                song = current_poll.get_highest_poll_song(self.stream_id)

            if(not song):
                logger.debug("no current poll or song , using a temporary song")
                song = Song.objects(track_n=158).get()

            song_url_path = song.path 
            
            retry_poll_creation = 3
            while(retry_poll_creation>0):
                poll = Poll.create_next_poll(self.stream_id)
                if(poll!=None):
                    break
                retry_poll_creation-=1
                
            if(poll==None):
                continue
            
            if(not song_url_path):
                song_url_path = "http://storage.googleapis.com/telugubeats_files/music/Telugu/devisri%20prasad/arya/amalapuram.mp3"
                
            if(song_url_path.startswith("/Users/abhinav/Desktop/Telugu//")):
                song_url_path="http://storage.googleapis.com/telugubeats_files/music/Telugu/"+urllib.quote(song_url_path[31:])
                
            logger.debug("playing ::" +song.title)
            self.fd = RemoteUrlFileObject(song_url_path)
            
            
            # additional info contains
            self.title = song.title
            self.image = song.album.image_url
            self.additional_info = json_util.dumps(song.to_son())
            self.save()
            
            self.publish_event(Event.NEW_SONG, str(song.id), from_user = None)
            self.publish_event(Event.NEW_POLL, str(poll.id), from_user = None)
            logger.debug("publishing new polls and song events")


            song.last_played = datetime.utcnow()
            song.save()

            
            stream_info_json = json_util.dumps(self.to_son())
            try:
                self.id3 = id3reader.Reader(self.fd)
                if isinstance(self.id3.header.size, int): # read out the id3 data
                    self.fd.seek(self.id3.header.size)
                    
                #insert a private frame with song_json_embedded
                private_bit_set_header = bytearray([0xFF, 0xFB, 0x93, 0x64])              
                num_song_info_chunks = (len(stream_info_json)*1.0)/414                
                mp3_frames_private= bytearray()
                i = 0
                while(i<num_song_info_chunks):
                    mp3_frames_private.extend(private_bit_set_header)
                    data_chunk = stream_info_json[414*i:414*i+414]
                    if(len(data_chunk)==414):
                        mp3_frames_private.extend(data_chunk)
                    else:
                        mp3_frames_private.extend(data_chunk)
                        mp3_frames_private.extend(bytearray([chr(0) for x in range(414-len(data_chunk))]))
                                                
                    i+=1
                    
                mp3_frames_private.extend(private_bit_set_header)#additional frame to ensure previous frames are read correctly as frames
                logger.debug("loaded new song , inserting data into private frame too")
                self.buffer.queue_chunk(mp3_frames_private)
                    
                while(True):
                    
                    try:
                        cur_time = time.time()
                        if(cur_time- last_sent_time_stamp > self.sleep_time):
                            last_sent_time_stamp = cur_time
                            self.buffer.queue_chunk(self.fd.read(self.buffer.chunk_byte_size))
                            gevent.sleep(self.sleep_time- time.time()+last_sent_time_stamp)
                    except EOFError:
                        # 1111 1111 1111 1011 1001 0011 0110 0100
                        # -289948
                        
                        
                        gevent.sleep(self.sleep_time)

                        self.fd.close()
                        break        
            except Exception as e:
                logger.debug("an error as occured"+ str(e))
    
    
    @audio_source_reader
    def forward_audio(self, socket):
        while(True):
            audio_data = bytearray()
            while(len(audio_data) < self.buffer.chunk_byte_size):
                audio_data.extend(bytes(socket.recv()))
                                
            self.buffer.queue_chunk(audio_data)            
        
    @audio_publishing_thread
    def stream_audio(self, socket):    
        for i in config.RESPONSE:
            socket.send(i)
        buffer, byte_rate , sleep_time = self.stream_buffers_info
        last_sent_time = time.time() # 4 chunks once  #16*chunks per second #bitrate , 16kbytes per second =>
        current_index =  buffer.get_current_head()-4
        max_chunk_diff = 20
        while True:
            cur_time = time.time()
            if(cur_time - last_sent_time> sleep_time):
                last_sent_time = cur_time
                chunk = buffer.get_chunk(current_index)
                #chunk = 32kb = > 16kbytes/sec => 1 chunks per 2 seconds 
                if((buffer.h)%buffer.size==current_index):# should not overtake
                    chunk = None
             
                if(chunk):
                    try:
                        n = 0
                        while(n<len(chunk)):    
                            n += socket.send(chunk[n:])
                        current_index+=1
                        current_index%=buffer.size
                        gevent.sleep( sleep_time - time.time()+last_sent_time)
                    except:
                        #client disconnected
                        break
                else:
                    gevent.sleep( sleep_time)
            else:
                gevent.sleep(sleep_time)



    def add_event_listener(self, socket):
        self.event_listeners[socket] = True
    
    
    
    def send_event(self, socket, data_to_send):
        try:
            response_write(socket , data_to_send)
            response_write(socket,"\r\n\r\n")
        except:
            del self.event_listeners[socket] # remove the socket
    
    
    # this is spawn as a new thread
    def start_publishing_events(self):
        while(True):
            # blocking call
            stream_event = self.event_queue.get()
                
            if(len(self.last_few_events)>20):
                self.last_few_events.pop(0)
                            
            self.last_few_events.append(stream_event)
            
            data_to_send = stream_event.to_json()

            for socket in self.event_listeners:
                # send data in parallel ?
                Greenlet.spawn(Stream.send_event , self, socket, data_to_send)
    
    def publish_event(self, event_id,  event_data , from_user=None):
        stream_event  = StreamEvent(event_id = event_id, data = event_data, from_user= from_user)
        stream_event.save()
        self.event_queue.put(stream_event)
        
    def to_son(self):
        return self.to_mongo()



# 
# class AudioStreamReader(Greenlet):
#     stream_id = None
#     fd = None
#     id3 = None
#     last_time_stamp  = time.time()
#     
#     ########## static stuff 
#     stream_buffers = {}
#     #bit rate = (319/8)*1024 bytes/s
#     #
#     
#     byte_rate = None#128kbps in bytes per second
#     sleep_time = None
#     # write to telugu buffer
#     def __init__(self, stream_id , bit_rate_in_kbps = 128.0):
#         Greenlet.__init__(self)
#         self.stream_id = stream_id
#         
#         if(not AudioStreamReader.stream_buffers.get(self.stream_id, None)):
#             buffer = Buffer()
#             byte_rate = ((bit_rate_in_kbps/8)*1024)
#             sleep_time = (buffer.chunk_byte_size*1.0)/byte_rate
#             AudioStreamReader.stream_buffers[stream_id] = [buffer , byte_rate, sleep_time]
#                         
#         self.buffer, self.byte_rate , self.sleep_time  = AudioStreamReader.stream_buffers[self.stream_id]
#         
#         
#     def _run(self):
#         print "loading audio stream :: ", self.stream_id
#         while(True):
#             song_url_path = None
#             current_poll = Poll.get_current_poll(self.stream_id)
#             if(current_poll):
#                 song = current_poll.get_highest_poll_song(self.stream_id)
#             if(IS_TEST_BUILD):
#                 song = Song.objects(track_n=158).get()
# 
# 
#             song_url_path = song.path 
#             retry_poll_creation = 3
#             while(retry_poll_creation>0):
#                 poll = Poll.create_next_poll(self.stream_id , not IS_TEST_BUILD)
#                 if(poll!=None):
#                     break
#                 retry_poll_creation-=1
#                 
#             if(poll==None):
#                 continue
#             
#             if(not song_url_path):
#                 song_url_path = "http://storage.googleapis.com/telugubeats_files/music/Telugu/devisri%20prasad/arya/amalapuram.mp3"
#                 
#             if(song_url_path.startswith("/Users/abhinav/Desktop/Telugu//")):                    
#                 song_url_path="http://storage.googleapis.com/telugubeats_files/music/Telugu/"+urllib.quote(song_url_path[31:])
#                 
#             print "playing::", song_url_path
#             self.fd = RemoteUrlFileObject(song_url_path)
#             
#             #spawn greenlet, keep reading into buffer
#             #block calls to seek and read if buffer is not sufficient enough
#             
#             
#             reset_data = InitData()
#             
#             reset_data.poll = poll
#             reset_data.n_user = 1000+len( stream_events_handler.event_listeners[self.stream_id])
#             reset_data.current_song  = song
#             song.last_played = datetime.utcnow()
#             if(not IS_TEST_BUILD):
#                 song.save()
# 
#             
#             song_info_json_string = json_util.dumps(song.to_son())
#             
#             event_data = json_util.dumps(reset_data.to_son())
#             
#             stream_events_handler.publish_event(self.stream_id, Event.RESET_POLLS_AND_SONG, event_data, from_user = None)
# 
#             # if there is valid ID3 data, read it out of the file first,
#             # so we can skip sending it to the client
#             try:
#                 self.id3 = id3reader.Reader(self.fd)
#                 if isinstance(self.id3.header.size, int): # read out the id3 data
#                     self.fd.seek(self.id3.header.size)
#                     
#                 #insert a private frame with song_json_embedded
#                 private_bit_set_header = bytearray([0xFF, 0xFB, 0x93, 0x64])              
#                 num_song_info_chunks = (len(song_info_json_string)*1.0)/414                
#                 mp3_frames_private= bytearray()
#                 i = 0
#                 print num_song_info_chunks
#                 while(i<num_song_info_chunks):
#                     mp3_frames_private.extend(private_bit_set_header)
#                     data_chunk = song_info_json_string[414*i:414*i+414]
#                     if(len(data_chunk)==414):
#                         mp3_frames_private.extend(data_chunk)
#                     else:
#                         mp3_frames_private.extend(data_chunk)
#                         mp3_frames_private.extend(bytearray([chr(0) for x in range(414-len(data_chunk))]))
#                                                 
#                     i+=1
#                     
#                 mp3_frames_private.extend(private_bit_set_header)#additional frame to ensure previous frames are read correctly as frames
#                 print mp3_frames_private
#                 self.buffer.queue_chunk(mp3_frames_private)
#                     
#                 while(True):
#                     
#                     try:
#                         cur_time = time.time()
#                         if(cur_time- self.last_time_stamp > self.sleep_time):
#                             self.last_time_stamp = cur_time
#                             self.buffer.queue_chunk(self.fd.read(self.buffer.chunk_byte_size))
#                             gevent.sleep(self.sleep_time- time.time()+self.last_time_stamp)
#                     except EOFError:
#                         # 1111 1111 1111 1011 1001 0011 0110 0100
#                         # -289948
#                         
#                         
#                         gevent.sleep(0.052)
# 
#                         self.fd.close()
#                         break        
#             except Exception as e:
#                     print e
#                     


        