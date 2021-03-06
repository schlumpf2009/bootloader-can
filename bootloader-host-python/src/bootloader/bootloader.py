#!/usr/bin/env python3
#
# Copyright (c) 2010, 2015-2017 Fabian Greif.
# All rights reserved.
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

""" Bootloader for AVR-Boards connected via CAN bus


Format:

11-Bit Identifier

1. Board Identifier
2. Message Type
3. Message Number
4. Message Data Counter
5.-8. Data

"""

import sys
import time
import math
import queue
import threading
import functools

from . import can
from . import message_filter as filter

from .util import progressbar

version = "1.5"


class BootloaderFilter(filter.BaseFilter):

    def check(self, message):
        if  message.extended == False and \
            message.rtr == False and \
            message.id == 0x7fe:
            return True
        return False


class BootloaderException(Exception):
    pass


class MessageSubject:
    # only available in type >= 2
    NO_OPERATION    = 0

    IDENTIFY        = 1
    SET_ADDRESS     = 2
    DATA            = 3
    START_APPLICATION = 4

    # only available in the extended types (>= 1)
    READ_FLASH      = 5
    GET_FUSEBITS    = 6
    CHIP_ERASE      = 7

    READ_EEPROM     = 8
    WRITE_EEPROM    = 9

    # only available in type >= 2
    SET_BOARD_ID    = 10
    SET_BITRATE     = 11

    # independent from bootloader
    START_BOOTLOADER = 127

    def __init__(self, subject):
        self.subject = subject

    def __str__(self):
        return { 0: "no_op",
                 1: "identify",
                 2: "set_address",
                 3: "data",
                 4: "start_app",
                 5: "read_flash",
                 6: "get_fusebit",
                 7: "chip_erase",
                 8: "read_eeprom",
                 9: "write_eeprom",
                 10: "set_board_id",
                 11: "set_bitrate",
                 127: "start_bootloader"}[self.subject]


class MessageType:
    REQUEST         = 0
    SUCCESS         = 1
    ERROR           = 2
    WRONG_NUMBER    = 3

    def __init__(self, messageType):
        self.type = messageType

    def __str__(self):
        return { 0: "request",
                 1: "success",
                 2: "error",
                 3: "wrong_number" }[self.type]


class Message:
    """ Representation of a message for the bootloader """

    BOOTLOADER_CAN_IDENTIFIER = 0x7ff
    START_OF_MESSAGE_MASK = 0x80

    def __init__(   self,
                    board_id = None,
                    messageType = MessageType.REQUEST,
                    subject = None,
                    number = 0,
                    data_counter = 0,
                    data = []):

        # set default values
        self.board_id = board_id
        self.type = messageType
        self.subject = subject
        self.number = number
        self.data_counter = data_counter
        self.data = data

    def decode(self, message):

        if len(message.data) < 4 or message.extended or message.rtr:
            raise BootloaderException("wrong format of message %s" % message)

        # convert can-message to a bootloader-message
        self.board_id = message.data[0]
        self.type = message.data[1] >> 6
        self.subject = message.data[1] & 0x3f
        self.number = message.data[2]
        self.data_counter = message.data[3]
        self.data = message.data[4:]

        return self

    def encode(self):
        """ Convert the bootloader-message to a can-message """

        data = [self.board_id, self.type << 6 | self.subject, self.number, self.data_counter] + self.data
        message = can.Message(self.BOOTLOADER_CAN_IDENTIFIER, data, extended = False, rtr = False)

        return message

    def __str__(self):
        s = "%s.%s id 0x%x [%x] %i >" % (MessageSubject(self.subject).__str__().upper(), MessageType(self.type), self.board_id, self.number, self.data_counter)
        for data in self.data:
            s += " %02x" % data

        return s


class ProgrammeableBoard:
    """Container class which holds information about an active board"""

    def __init__(self, identifier):
        self.id = identifier
        self.connected = False

        # information about the board we are currently programming
        self.bootloader_type = None
        self.version = 0.0
        self.pages = 0
        self.pagesize = 0

    def __str__(self):
        s = "board id %d (0x%x)" % (self.id, self.id)
        if self.connected:
            s += " (T%i) v%1.1f, %i pages [%i Byte]" % (self.bootloader_type, self.version, self.pages, self.pagesize)
        return s


class Bootloader:

    WAITING = 0
    START = 1
    IN_PROGRESS = 2
    END = 3
    ERROR = 4

    def __init__(self, board_id, interface, debug = False):
        """Constructor"""

        self.board = ProgrammeableBoard(board_id)

        # connect to the message dispatcher
        bootfilter = BootloaderFilter(self._get_message)
        self.interface = interface
        self.interface.addFilter(bootfilter)

        self.debugmode = debug

        self.msg_number = 0
        self.msg_wait_for = threading.Event()
        self.msg_queue = queue.Queue()

    def _start_bootloader_command(self):
        pass

    def scan(self):
        board_backup = self.board

        class Srubber(threading.Thread):
            def __init__(self, bootloader):
                threading.Thread.__init__(self)
                self.bootloader = bootloader
                self.finish = threading.Event()

            def run(self):
                while not self.finish.is_set():
                    try:
                        self.bootloader.board = ProgrammeableBoard(0)
                        self.bootloader.msg_number = 0
                        self.bootloader._send(subject = MessageSubject.NO_OPERATION, response=False)

                        time.sleep(0.05)
                    except BootloaderException:
                        pass

        scrubber = Srubber(self)
        scrubber.start()

        input('Press "Enter" to start scan')

        scrubber.finish.set()
        scrubber.join()

        found = []
        for i in range(1, 255):
            try:
                self.board = ProgrammeableBoard(0)
                self.msg_number = 0
                response = self._send(subject = MessageSubject.NO_OPERATION, response=False)

                self.board = ProgrammeableBoard(i)

                # Try to start the bootloader
                self.start_bootloader()
                time.sleep(0.02)

                self.msg_number = 0
                response = self._send(subject=MessageSubject.IDENTIFY, timeout=0.1, attempts=2)

                self._decode_response_identify(response, self.board)
                self.board.connected = True
                print()
                print("Found:", self.board)

                found.append(self.board.id)
            except BootloaderException:
                print(".", end="")
                sys.stdout.flush()

        print("\nAll board identifiers scanned. Found %i boards." % len(found))

        # Restore original setting
        self.board = board_backup
        return found

    def identify(self):
        """
        Send the "Identify" command until it gets a response from the
        bootloader and decode the returned information
        """

        # send message and wait for a response
        while True:
            try:
                self._start_bootloader_command()
                response = self._send(subject = MessageSubject.IDENTIFY, timeout = 0.1, attempts = 10)
            except BootloaderException:
                pass
            else:
                break

        self._decode_response_identify(response, self.board)
        self.board.connected = True

    def _decode_response_identify(self, response, board):
        # split up the message and fill in the board-representation
        board.bootloader_type = response.data[0] >> 4
        board.version = response.data[0] & 0x0F

        board.pagesize = {0: 32, 1: 64, 2: 128, 3: 256}[response.data[1]]
        board.pages = (response.data[2] << 8) + response.data[3]

    def program_page(self, page, data, addressAlreadySet = False):
        """
        Program a page of the flash memory

        Tries the send the data in a blocks of 32 messages befor an
        acknowledge. The blocksize is stepwise reduced to one when there
        are any errors during the transmission.
        Raises BootloaderException if the error stil appears then.
        """
        data = [ord(x) for x in data]

        # amend the data field to a complete page
        size = len(data)
        if size < self.board.pagesize:
            data += [0xff] * (self.board.pagesize - size)

        remaining = self.board.pagesize / 4
        blocksize = 64
        offset = 0

        while remaining > 0:
            try:
                if not addressAlreadySet:
                    # set address in the page buffer
                    self._send( subject=MessageSubject.SET_ADDRESS, data=[page >> 8, page & 0xff, 0, offset] )

                if remaining < blocksize:
                    blocksize = remaining

                if blocksize == 1:
                    answer = self._send( subject=MessageSubject.DATA, data=data[offset*4:offset*4 + 4] )
                else:
                    i = offset

                    # start of a new block
                    self._send( subject=MessageSubject.DATA,
                                response=False,
                                counter=Message.START_OF_MESSAGE_MASK | (blocksize - 1),
                                data=data[i * 4: i * 4 + 4])

                    for k in range(blocksize - 2, 0 , -1):
                        i += 1
                        self._send( subject=MessageSubject.DATA,
                                    response=False,
                                    counter=k,
                                    data=data[i*4:i*4 + 4] )

                    # wait for the response for the last message of this block
                    i += 1
                    answer = self._send( subject=MessageSubject.DATA,
                                response=True,
                                counter=0,
                                data=data[i * 4: i * 4 + 4])

                remaining -= blocksize
                offset += blocksize

                addressAlreadySet = True

            except BootloaderException as msg:
                print("Exception: %s" % msg)
                if blocksize > 1:
                    blocksize /= 2
                    print(blocksize)

                    # we have to reset the buffer position
                    addressAlreadySet = False

                    time.sleep(0.3)
                else:
                    raise

        # check whether the page was written correctly
        returned_page = answer.data[0] << 8 | answer.data[1]

        if returned_page != page:
            raise BootloaderException("Could not write page %i!" % page)

        # page was completly transmitted => write it to the flash
        #self._send( MessageSubject.WRITE_PAGE, [page / 0xff, page % 0xff] )

    def verify_page(self, page, data):
        """
        Verify a page of the flash memory
        """
        data = [ord(x) for x in data]

        # amend the data field to a complete page
        size = len(data)
        if size < self.board.pagesize:
            data += [0xff] * (self.board.pagesize - size)

        remaining = self.board.pagesize / 4
        offset = 0

        while remaining > 0:
            block = data[offset*4:offset*4 + 4]
            answer = self._send(subject=MessageSubject.READ_FLASH,
                                data=[page >> 8, page & 0xff, 0, offset])

            if block != answer.data:
                raise BootloaderException("Could not write page %i!" % page)

            remaining -= 1
            offset += 1

    def start_app(self):
        """Start the written application"""
        self._send( MessageSubject.START_APPLICATION )

    def program(self, segments):
        """
        Program the AVR

        First the function waits for a connection then it will send the
        data page by page.
        """
        self._report_progress(self.WAITING)

        print("connecting ... ", end="")
        sys.stdout.flush()

        # try to connect to the bootloader
        self.identify()

        print("ok")
        print(self.board)

        totalsize = functools.reduce(lambda x,y: x + y, map(lambda x: len(x), segments))
        segment_number = 0

        pagesize = self.board.pagesize
        pages = int(math.ceil(float(totalsize) / float(pagesize)))

        print("write %i pages\n" % pages)
        print("Program:")

        if pages > self.board.pages:
            raise BootloaderException("Programsize exceeds available Flash!")

        # start progressbar
        self._report_progress(self.START)
        starttime = time.time()
        addressSet = False
        offset = 0

        for i in range(pages):
            data = segments[segment_number]
            self.program_page(page = i,
                              data = data[offset:offset+pagesize],
                              addressAlreadySet = addressSet)
            offset += pagesize
            if offset >= len(data):
                offset = 0
                segment_number += 1
                self.debug("Now starting segment %i" % segment_number)
            addressSet = True
            self._report_progress(self.IN_PROGRESS, float(i) / float(pages))

        # show a 100% progressbar
        self._report_progress(self.END)

        endtime = time.time()
        totaltime = endtime - starttime
        transferrate = int(totalsize / totaltime)
        print("%.2f seconds (%i Byte/s)\n" % (totaltime, transferrate))

    def verify(self, segments):
        """
        Verify the program on the AVR

        First the function waits for a connection then it will send the
        data page by page. Finally the written application will be started.
        """
        self._report_progress(self.WAITING)

        # try to connect to the bootloader
        self.identify()

        totalsize = functools.reduce(lambda x,y: x + y, map(lambda x: len(x), segments))
        segment_number = 0

        pagesize = self.board.pagesize
        pages = int(math.ceil(float(totalsize) / float(pagesize)))

        if self.board.bootloader_type == 0:
            raise BootloaderException("Verify requires an extended Bootloader. Aborting!")

        if pages > self.board.pages:
            raise BootloaderException("Programsize exceeds available Flash!")

        print("Verify:")

        # start progressbar
        self._report_progress(self.START)
        starttime = time.time()
        offset = 0

        for i in range(pages):
            data = segments[segment_number]
            self.verify_page(page = i,
                             data = data[offset:offset+pagesize])
            offset += pagesize
            if offset >= len(data):
                offset = 0
                segment_number += 1
                self.debug("Now starting segment %i" % segment_number)
            self._report_progress(self.IN_PROGRESS, float(i) / float(pages))

        # show a 100% progressbar
        self._report_progress(self.END)

        endtime = time.time()
        totaltime = endtime - starttime
        transferrate = int(totalsize / totaltime)
        print("%.2f seconds (%i Byte/s)\n" % (totaltime, transferrate))

    def set_board_id(self, new):
        self.board.connected = True
        self.msg_number = 0

        self._send(subject=MessageSubject.SET_BOARD_ID, data=[new], timeout=0.05, attempts=0)

    def start_bootloader(self):
        """
        Start the bootloader.

        Only works if the main application supports this.
        """
        message = Message(board_id = self.board.id,
                          messageType = MessageType.REQUEST,
                          subject = MessageSubject.START_BOOTLOADER,
                          number = 0,
                          data_counter = 0,
                          data = [] )
        self.interface.send(message.encode())

    def _send(self,
              subject,
              data = [],
              counter = Message.START_OF_MESSAGE_MASK | 0,
              response = True,
              timeout = 0.5,
              attempts = 2):

        """
        Send a message via CAN Bus

        With default settings the functions waits for the response to the
        message and retry the transmission after a timeout. After the
        specifed number of retries it will raise a BootloaderException.

        Keeps track of the message numbering and restores the correct number
        in case of a reported error.
        """

        message = Message(board_id = self.board.id,
                          messageType = MessageType.REQUEST,
                          subject = subject,
                          number = self.msg_number,
                          data_counter = counter,
                          data = data )

        if not response:
            # no response needed, just send the message and return
            self.interface.send( message.encode() )
            self.msg_number = (self.msg_number + 1) & 0xff
            return None

        repeats = 0
        finished = False

        # clear message queue to delete messages belonging to another
        # transmission
        while True:
            try:
                self.msg_queue.get(False, 0)
            except queue.Empty:
                break

        while not finished:
            # send the message
            self.interface.send(message.encode())

            # wait for the response
            while True:
                try:
                    response_msg = self.msg_queue.get(block=True, timeout=timeout)
                except queue.Empty:
                    break
                else:
                    if response_msg.subject == message.subject:
                        if response_msg.type == MessageType.SUCCESS:
                            finished = True

                            # drain message queue to delete answers to repeated transmits
                            while True:
                                try:
                                    self.msg_queue.get(block=False, timeout=0)
                                except queue.Empty:
                                    break

                            break
                        elif response_msg.type == MessageType.WRONG_NUMBER:
                            self.debug("Warning: Wrong message number detected (board: 0x%02x, here: 0x%02x)" %
                                    (response_msg.number, message.number))

                            # reset message number only if we just started the communication
                            resetted = False
                            if message.number == 0:
                                self.debug("Reset to 0x%02x" % response_msg.number)
                                self.msg_number = response_msg.number
                                message.number = response_msg.number
                                resetted = True

                            # wait a bit for other error messages
                            time.sleep(0.1)
                            while True:
                                try:
                                    self.msg_queue.get(block=False, timeout=0)
                                except queue.Empty:
                                    break
                            # TODO reset command stack?
                            # target might have cycled power, so send address for next block
                            #addressAlreadySet = False
                            if not resetted:
                                break
                        else:
                            raise BootloaderException("Failure %i while sending '%s'" %
                                                        (response_msg.type, message))
                    else:
                        self.debug("Warning: Discarding obviously old message (received %i/%x, here: %i/%x)" %
                                    (response_msg.subject, response_msg.number, message.subject, message.number))

            repeats += 1
            if not finished and (attempts > 0 and repeats >= attempts):
                raise BootloaderException("No response after %i attempts and timeout %.2f while sending '%s'" %
                                            (repeats, timeout, message))

        # increment the message number
        self.msg_number = (self.msg_number + 1) & 0xff
        return response_msg

    def _get_message(self, can_message):
        """Receives and checks all messages from the CAN bus"""
        self.debug("> " + str(can_message))
        try:
            message = Message().decode(can_message)
            if message.board_id != self.board.id:
                # message is for someone other
                return
        except BootloaderException:
            # message has an incorrect format
            return

        self.msg_queue.put(message)

    def debug(self, text):
        if self.debugmode:
            print(text)

    def _report_progress(self, state, progress = 0.0):
        """Called to report the current status

        Can be overwritten to implement a progressbar for example.
        """
        pass


class CommandlineClient(Bootloader):

    def __init__(self, board_id, interface, debug):
        Bootloader.__init__(self, board_id, interface, debug)

        # create a progressbar to show the progress while programming
        self.progressbar = progressbar.ProgressBar(max = 1.0, width = 60)

    def _start_bootloader_command(self):
        # send a rccp reset command
        dest = self.board.id
        source = 0xff
        identifier = "0x18%02x%02x%02x" % (dest, source, 0x01)

        msg = can.Message(int(identifier, 16), extended = True, rtr = False)
        self.interface.send(msg)

    def _report_progress(self, state, progress = 0.0):
        if state == self.WAITING:
            pass
        elif state == self.START:
            self.progressbar(0)
        elif state == self.IN_PROGRESS:
            self.progressbar(progress)
        elif state == self.END:
            self.progressbar(1.0)
            print("")
