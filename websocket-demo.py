#!/usr/bin/env python

from __future__ import absolute_import, print_function

import io
import logging
import os
import sys
import time
from logging import debug, info
import uuid
import cgi
import nexmo

import requests
import tornado.ioloop
import tornado.websocket
import tornado.httpserver
import tornado.template
import tornado.web
import webrtcvad
from tornado.web import url
import json

from base64 import b64decode

#Only used for record function
import datetime
import wave

import numpy as np
from scipy.io import wavfile
import librosa
import pickle
from google.cloud import storage

from dotenv import load_dotenv
from sklearn.naive_bayes import GaussianNB

load_dotenv()

os.environ['KMP_DUPLICATE_LIB_OK']='True'

logging.captureWarnings(True)


# Constants:
MS_PER_FRAME = 15  # Duration of a frame in ms
HOSTNAME =  os.getenv("HOSTNAME")#Change to the hostname of your server
NEXMO_NUMBER = os.getenv("NEXMO_NUMBER")
NEXMO_APP_ID = os.getenv("NEXMO_APP_ID")
CONF_NAME = os.getenv("CONF_NAME")

GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_PROJECT__PRIVATE_KEY_ID=os.getenv("GCP_PROJECT__PRIVATE_KEY_ID")
GCP_PROJECT_PRIVATE_KEY=os.getenv("GCP_PROJECT_PRIVATE_KEY")

GCP_PROJECT_CLIENT_EMAIL=os.getenv("GCP_PROJECT_CLIENT_EMAIL")
GCP_PROJECT_CLIENT_ID=os.getenv("GCP_PROJECT_CLIENT_ID")
GCP_PROJECT_AUTH_URI=os.getenv("GCP_PROJECT_AUTH_URI")
GCP_PROJECT_TOKEN_URI=os.getenv("GCP_PROJECT_TOKEN_URI")
GCP_PROJECT_AUTH_PROVIDER_CERT_URL=os.getenv("GCP_PROJECT_AUTH_PROVIDER_CERT_URL")
GCP_PROJECT_CERT_URL=os.getenv("GCP_PROJECT_CERT_URL")

storage_client = storage.Client(os.getenv("PROJECT_ID"))
bucket = storage_client.get_bucket(os.getenv("CLOUD_STORAGE_BUCKET"))

# Global variables
conns = {}
clients = []
conversation_uuids = dict()

loaded_model = pickle.load(open("models/GaussianNB-20190130T1233.pkl", "rb"))
print(loaded_model)
client = nexmo.Client(application_id=NEXMO_APP_ID, private_key=NEXMO_APP_ID+".key")
print(client)
class BufferedPipe(object):
    def __init__(self, max_frames, sink):
        """
        Create a buffer which will call the provided `sink` when full.

        It will call `sink` with the number of frames and the accumulated bytes when it reaches
        `max_buffer_size` frames.
        """
        self.sink = sink
        self.max_frames = max_frames

        self.count = 0
        self.payload = b''

    def append(self, data, id):
        """ Add another data to the buffer. `data` should be a `bytes` object. """

        self.count += 1
        self.payload += data

        if self.count == self.max_frames:
            self.process(id)

    def process(self, id):
        """ Process and clear the buffer. """
        self.sink(self.count, self.payload, id)
        self.count = 0
        self.payload = b''

class LexProcessor(object):
    def __init__(self, path, rate, clip_min, uuid):
        self.rate = rate
        self.bytes_per_frame = rate/25
        self._path = path
        self.clip_min_frames = clip_min // MS_PER_FRAME
        self.uuid = uuid
    def process(self, count, payload, id):
        if count > self.clip_min_frames:  # If the buffer is less than CLIP_MIN_MS, ignore it
            fn = "{}rec-{}-{}.wav".format('', id, datetime.datetime.now().strftime("%Y%m%dT%H%M%S"))
            output = wave.open(fn, 'wb')
            output.setparams((1, 2, self.rate, 0, 'NONE', 'not compressed'))
            output.writeframes(payload)
            output.close()
            debug('File written {}'.format(fn))
            self.process_file(fn)
            info('Processing {} frames for {}'.format(str(count), id))
            self.removeFile(fn)
        else:
            info('Discarding {} frames'.format(str(count)))
    def process_file(self, wav_file):
        if loaded_model != None:
            print("load file {}".format(wav_file))

            X, sample_rate = librosa.load(wav_file, res_type='kaiser_fast')
            mfccs = np.mean(librosa.feature.mfcc(y=X, sr=sample_rate, n_mfcc=40).T,axis=0)
            X = [mfccs]
            prediction = loaded_model.predict(X)
            print("prediction",prediction)

            if prediction[0] == 0:
                beep_captured = True
                print("beep detected")
            else:
                beep_captured = False

            for client in clients:
                print(client)
                client.write_message({"uuid":self.uuid, "beep_detected":beep_captured})

        else:
            print("model not loaded")
    def removeFile(self, wav_file):
         os.remove(wav_file)

class WSHandler(tornado.websocket.WebSocketHandler):
    def initialize(self):
        # Create a buffer which will call `process` when it is full:
        self.frame_buffer = None
        # Setup the Voice Activity Detector
        self.tick = None
        self.id = uuid.uuid4().hex
        self.vad = webrtcvad.Vad()
          # Level of sensitivity
        self.processor = None
        self.path = None
        self.rate = None #default to None
        self.silence = 20 #default of 20 frames (400ms)
        conns[self.id] = self
    def open(self, path):
        info("client connected")
        clients.append(self)
        debug(self.request.uri)
        self.path = self.request.uri
        self.tick = 0
    def on_message(self, message):
        # Check if message is Binary or Text
        if type(message) != str:
            # print(self.rate)
            if self.vad.is_speech(message, self.rate):
                debug ("SPEECH from {}".format(self.id))
                self.tick = self.silence
                self.frame_buffer.append(message, self.id)
            else:
                debug("Silence from {} TICK: {}".format(self.id, self.tick))
                self.tick -= 1
                if self.tick == 0:
                    self.frame_buffer.process(self.id)  # Force processing and clearing of the buffer
        else:
            # Here we should be extracting the meta data that was sent and attaching it to the connection object
            data = json.loads(message)
            print("on_message",data)
            if data.get('content-type'):
                m_type, m_options = cgi.parse_header(data['content-type'])
                self.rate = 16000
                # region = data.get('aws_region', 'us-east-1')
                clip_min = int(data.get('clip_min', 200))
                clip_max = int(data.get('clip_max', 10000))
                silence_time = int(data.get('silence_time', 300))
                sensitivity = int(data.get('sensitivity', 3))
                uuid = data.get('uuid')
                self.vad.set_mode(sensitivity)
                self.silence = silence_time // MS_PER_FRAME
                self.processor = LexProcessor(self.path, self.rate, clip_min, uuid).process
                self.frame_buffer = BufferedPipe(clip_max // MS_PER_FRAME, self.processor)
                self.write_message('ok')
    def on_close(self):
        print("close")
        # Remove the connection from the list of connections
        del conns[self.id]
        clients.remove(self)
        info("client disconnected")


class PingHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        self.write('ok')
        self.set_header("Content-Type", 'text/plain')
        self.finish()

class EventHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        # print("event:", self.request.body)

        data = json.loads(self.request.body)
        try:
            ""
            if data["status"] == "answered" and "ws://" in data["to"]:
                print("found WS")
                uuid = data["uuid"]
                conversation_uuid = data["conversation_uuid"]
                conversation_uuids[conversation_uuid] = uuid
        except:
            pass


        try:
            if data["status"] == "completed":
                ws_conversation_id = conversation_uuids[data["conversation_uuid"]]

                print(conversation_uuids[data["conversation_uuid"]])
                response = client.update_call(ws_conversation_id, action='hangup')
                conversation_uuids[data["conversation_uuid"]] = ''
                print(response)

        except Exception as e:
            print(e)
            pass


        self.content_type = 'text/plain'
        self.write('ok')
        self.finish()

class EnterPhoneNumberHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        ncco = [
              {
                "action": "talk",
                "text": "Please enter a phone number to dial"
              },
              {
                "action": "input",
                "eventUrl": ["https://"+HOSTNAME+"/ivr"],
                "timeOut":10,
                "maxDigits":12,
                "submitOnHash":True
              }

            ]
        self.write(json.dumps(ncco))
        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.finish()


class AcceptNumberHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        data = json.loads(self.request.body)
        ncco = [
              {
                "action": "talk",
                "text": "Thanks. Connecting you now"
              },
            {
                "action": "record",
                "eventUrl": ["https://"+HOSTNAME+"/recording"],
              },
             {
             "action": "connect",
              "eventUrl": ["https://"+HOSTNAME+"/event"],
               "from": NEXMO_NUMBER,
               "endpoint": [
                 {
                   "type": "phone",
                   "number": data["dtmf"]
                 }
               ]
             },
              {
                 "action": "connect",
                 "eventUrl": ["https://"+HOSTNAME+"/event"],
                 "from": NEXMO_NUMBER,
                 "endpoint": [
                     {
                        "type": "websocket",
                        "uri" : "ws://"+HOSTNAME+"/socket",
                        "content-type": "audio/l16;rate=16000",
                        "headers": {
                            "uuid":data["uuid"]
                        }
                     }
                 ]
               }
            ]
        self.write(json.dumps(ncco))
        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.finish()

class CallHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self):
        ncco = [

            {
               "action": "connect",
               "eventUrl": ["https://"+HOSTNAME+"/event"],
               "from": NEXMO_NUMBER,
               "endpoint": [
                   {
                      "type": "websocket",
                      "uri" : "ws://"+HOSTNAME+"/socket",
                      "content-type": "audio/l16;rate=16000",
                      "headers": {
                      }
                   }
               ]
             },
             {
             "action": "conversation",
             "name": CONF_NAME
             }
        ]
        self.write(json.dumps(ncco))
        self.set_header("Content-Type", 'application/json; charset="utf-8"')
        self.finish()

class RecordHandler(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def post(self):
        data = json.loads(self.request.body)

        response = client.get_recording(data["recording_url"])
        fn = "call-{}.wav".format(data["conversation_uuid"])

        try:
            blob = bucket.blob(fn)
            blob.upload_from_string(response, content_type="audio/wav")
            print('File uploaded.')
        except Exception as e:
            print("Error encountered while uploading file: ", e)


        self.write('ok')
        self.set_header("Content-Type", 'text/plain')
        self.finish()

def main():

    GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
    GCP_PROJECT__PRIVATE_KEY_ID=os.getenv("GCP_PROJECT__PRIVATE_KEY_ID")
    GCP_PROJECT_PRIVATE_KEY=os.getenv("GCP_PROJECT_PRIVATE_KEY")

    GCP_PROJECT_CLIENT_EMAIL=os.getenv("GCP_PROJECT_CLIENT_EMAIL")
    GCP_PROJECT_CLIENT_ID=os.getenv("GCP_PROJECT_CLIENT_ID")
    GCP_PROJECT_AUTH_URI=os.getenv("GCP_PROJECT_AUTH_URI")
    GCP_PROJECT_TOKEN_URI=os.getenv("GCP_PROJECT_TOKEN_URI")
    GCP_PROJECT_AUTH_PROVIDER_CERT_URL=os.getenv("GCP_PROJECT_AUTH_PROVIDER_CERT_URL")
    GCP_PROJECT_CERT_URL=os.getenv("GCP_PROJECT_CERT_URL")

    goole_auth = {
      "type": "service_account",
      "project_id": GCP_PROJECT_ID,
      "private_key_id": GCP_PROJECT__PRIVATE_KEY_ID,
      "private_key": GCP_PROJECT_PRIVATE_KEY,
      "client_email": GCP_PROJECT_CLIENT_EMAIL,
      "client_id": GCP_PROJECT_CLIENT_ID,
      "auth_uri": GCP_PROJECT_AUTH_URI,
      "token_uri": GCP_PROJECT_TOKEN_URI,
      "auth_provider_x509_cert_url": GCP_PROJECT_AUTH_PROVIDER_CERT_URL,
      "client_x509_cert_url": GCP_PROJECT_CERT_URL
    }

    import json
    with open('amd-recordings-firebase-adminsdk-eweq5-7b854cec310.json', 'w') as outfile:
        json.dump(goole_auth, outfile)

    try:
        blob = bucket.blob("TEST")
        blob.upload_from_string("TEST")
        print('File uploaded.')
    except Exception as e:
        print("Error encountered while uploading file: ", e)

    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)7s %(message)s",
        )
        application = tornado.web.Application([
			url(r"/ping", PingHandler),
            (r"/event", EventHandler),
            (r"/ncco", EnterPhoneNumberHandler),
            (r"/ncco-connect", CallHandler),
            (r"/recording", RecordHandler),
            (r"/ivr", AcceptNumberHandler),
            url(r"/(.*)", WSHandler),
        ])
        http_server = tornado.httpserver.HTTPServer(application)
        port = int(os.getenv('PORT', 8000))
        http_server.listen(port)
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        pass  # Suppress the stack-trace on quit


if __name__ == "__main__":

    main()
