# Copyright (c) 2016, NVIDIA CORPORATION.  All rights reserved.
#
# This document should comply with PEP-8 Style Guide
# Linter: pylint

"""
Interface for data loading for Tensorflow.
Data loading is done through a data loading factory,that will setup
the correct functions for the respective backends.

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from PIL import Image
import logging
import lmdb
import magic
import math
import numpy as np
import os
import sys
import tensorflow as tf

# Local imports
import caffe_tf_pb2
import utils as digits

# Constants
MIN_FRACTION_OF_EXAMPLES_IN_QUEUE = 0.4
MAX_ABSOLUTE_EXAMPLES_IN_QUEUE = 4096 # Maybe set this to 10x batch size?
NUM_THREADS_DATA_LOADER = 4
LOG_MEAN_FILE = False # Logs the mean file as loaded in TF to TB

# Supported extensions for Loaders
HDF5_EXT = ['.h5','.hdf5']
LMDB_EXT = ['.mdb','.lmdb']
IMG_FILE_EXT = ['.jpg','.png']
FILELIST_EXT = ['.txt']
LIST_DELIMITER = ' '

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s',datefmt='%Y-%m-%d %H:%M:%S', level=logging.INFO)

def get_backend_of_source(db_path):
    """
    Takes a path as argument and infers the format of the data
    Args:
        db_path: path to a file or directory
    Returns:
        backend: the backend type
    """

    file_path = None

    if os.path.isdir(db_path):
        # Select a single file from the directory to test the extension on later
        for f in os.listdir(db_path):
            if not f.startswith('.'):
                file_path = db_path + '/' + f
                break
        if not file_path:
            logging.error("File source directory (%s) empty." % (db_path))
            exit(-1)
    else:
        file_path = db_path

    if os.path.isfile(file_path):
        _, file_extension = os.path.splitext(file_path)
        if file_extension in HDF5_EXT:
            return 'hdf5'
        elif file_extension in LMDB_EXT:
            return 'lmdb'
        elif file_extension in FILELIST_EXT:
            return 'filelist'
        elif file_extension in IMG_FILE_EXT:
            return 'file'
    else:
        logging.error("No file found for infering backend (%s)." % (file_path))
        exit(-1)

    # Unknown backend.
    return None


    #/Users/tzaman/Dropbox/code/DIGITS/digits/jobs/20160615-215643-75fd/train_db

class MeanLoader(object):
    """
    Loads in a mean file for tensorflow. This is done through using a constant
    variable. It needs to be loaded first, after which the constant tf op
    can be retrieved through a function, and can be accounted for.

    """
    def __init__(self, mean_file_path, subtraction_type, bitdepth):
        self._mean_file_path = mean_file_path
        self._subtraction_type = subtraction_type
        self._bitdepth = bitdepth
        self.tf_mean_image = None
        self.load_mean()

    def load_mean(self):
        """
        The mean is loaded in the graph through a tf.constant for maximum efficiency. This is first
        done only once through a numpy array that defines the value of the constant.
        All pre-processing of the mean file is done before the definition of the tf.constant
        to make sure these operations are not repeated in the graph
        """

        _, file_extension = os.path.splitext(self._mean_file_path)

        if file_extension == '.binaryproto':
            blob = caffe_tf_pb2.BlobProto()
            with open(self._mean_file_path, 'rb') as infile:
                blob.ParseFromString(infile.read())
            data = np.array(blob.data, dtype="float32").reshape(blob.channels, blob.height, blob.width)
            if blob.channels == 3:
                # converting from BGR to RGB
                data = data[[2,1,0],...] # channel swap
                # convert to (height, width, channels)
                data = data.transpose((1,2,0))
            elif blob.channels == 1:
                # convert to (height, width)
                data = data[0]
            else:
                logging.error('Unknown amount of channels (%d) in mean file (%s)' % (blob.channels, self._mean_file_path))
                exit(-1)
        elif file_extension in IMG_FILE_EXT:
            img = Image.open(self._mean_file_path)
            img.load()
            data = np.asarray(img, dtype="float32")
        else:
            logging.error('Failed loading mean file: Unsupported extension (%s)' % (file_extension))
            exit(-1)

        if (self._subtraction_type == 'image') or (self._subtraction_type == 'pixel'):
            if self._subtraction_type == 'pixel':
                data = data.mean(axis=(0, 1))
                data = np.reshape(data, (1, 1, -1))
            elif len(data.shape) != 3:
                # Explicitly add channel dim
                data = data[:, :, None]

            # Normalize to [0:1]
            if self._bitdepth == 16:
                data = data / 65535
            else:
                data = data / 255
            self.tf_mean_image = tf.constant(data, name='Const_Mean_Image')

        else:
            logging.error('Unsupported mean subtraction type (%s)' % (self._subtraction_type))
            exit(-1)

    def subtract_mean_op(self, tf_graph):
        """
        Places mean subtraction on top of the tensorflow graph supplied, returns the added op
        Args:
            tf_graph: the graph the subtraction of the mean should placed upon
        Returns:
            The graph with the mean subtraction placed on top of it
        """
        return (tf_graph - self.tf_mean_image)


class LoaderFactory(object):
    """
    A factory for data loading. It sets up a subclass with data loading
    done with the respective backend. Its output is a tensorflow queue op
    that is used to load in data, with optionally some minor postprocessing ops.
    """
    def __init__(self):
        self.croplen = None
        self.nclasses = None
        self.mean_loader = None
        self.backend = None
        self.db_path = None
        self.batch_x = None
        self.batch_y = None
        self.batch_k = None
        self.stage = None
        self._seed = None

        self.summaries = None
        pass

    @staticmethod
    def set_source(db_path):
        """
        Returns the correct backend.
        """ 
        backend = get_backend_of_source(db_path)
        loader = None
        if backend == 'lmdb':
            loader = LmdbLoader()
        elif backend == 'hdf5':
            loader = Hdf5Loader()
        elif backend == 'file' or backend == 'filelist':
            loader = FileListLoader()
        else:
            logging.error("Backend (%s) not implemented" % (backend))
            exit(-1)
        loader.backend = backend
        loader.db_path = db_path
        return loader

    def setup(self, labels_db_path, shuffle, bitdepth, batch_size, num_epochs=None, seed=None):
        self.labels_db_path = labels_db_path

        self.shuffle = shuffle
        self.bitdepth = bitdepth
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        self._seed = seed

        if self.labels_db_path:
            self.labels_db = LoaderFactory.set_source(self.labels_db_path)
            self.labels_db.bitdepth = self.bitdepth
            self.labels_db.stage = self.stage
            self.labels_db.initialize()

        self.initialize()
        logging.info("Found %s images in db %s ", self.get_total(), self.db_path)

    def get_key_index(self, key):
        return self.keys.index(key)

    def set_augmentation(self, mean_loader):
        self.mean_loader = mean_loader

    def get_shape(self):
        input_shape = [self.width, self.height, self.channels]
        # update input_shape if crop length specified
        # this is necessary as the input_shape is provided
        # below to the user-defined function that defines the network
        if self.croplen > 0:
            input_shape[0] = self.croplen
            input_shape[1] = self.croplen
        return input_shape

    def get_total(self):
        return self.total

    def reshape_decode(self, data, shape):
        if self.float_data: #@TODO(tzaman): this is LMDB specific - Make generic!
            data = tf.reshape(data, shape)
        else:
            # Decode image of any time option might come: https://github.com/tensorflow/tensorflow/issues/4009
            # Distinguish between mime types
            if self.data_encoded:
                if self.data_mime == 'image/png':
                    data = tf.image.decode_png(data, dtype=self.image_dtype, name='image_decoder')
                elif self.data_mime == 'image/jpeg':
                    data = tf.image.decode_jpeg(data, name='image_decoder')
                else:
                    logging.error('Unsupported mime type (%s); cannot be decoded' % (self.data_mime))
                    exit(-1)
            else:
                data = tf.decode_raw(data, self.image_dtype, name='raw_decoder')
                data = tf.reshape(data, shape) # Dynamic reshaping

            # Convert to float
            data = tf.image.convert_image_dtype(data, tf.float32) # Converts to [0:1) range
        return data

    def create_input_pipeline(self):
        """
        This function returns part of the graph that does data loading, and
        includes a queueing, optional data decoding and optional post-processing
        like data augmentation or mean subtraction.

        Args:
            None.
        Produces:
            batch_x: Input data batch
            batch_y: Label data batch
            batch_k: A list of keys (strings) from which the batch originated
        Returns:
            None.
        """
    
        # @TODO(tzaman) the container can be used if the reset function is implemented:
        # see https://github.com/tensorflow/tensorflow/issues/4535#issuecomment-248990633
        #
        #with tf.container('queue-container'): 
        
        key_queue = tf.train.string_input_producer(
            self.keys,
            num_epochs=self.num_epochs,
            capacity=self.total,
            shuffle=self.shuffle,
            seed=self._seed,
            name='input_producer'
            )

        single_label = None
        single_label_shape = None
        if self.stage == digits.STAGE_INF:
            single_key, single_data, single_data_shape = self.get_single_data(key_queue)
        else:
            single_key, single_data, single_data_shape, single_label, single_label_shape = self.get_single_data(key_queue)
        
        single_data_shape = tf.reshape(single_data_shape, [3]) # Shape the shape
        single_data = self.reshape_decode(single_data, single_data_shape)

        if self.labels_db_path: # Using a seperate label db; label can be anything
            single_label_shape = tf.reshape(single_label_shape, [3])  # Shape the shape
            single_label = self.labels_db.reshape_decode(single_label, single_label_shape)
        elif single_label is not None: # Not using a seperate label db; label is a scalar
            single_label = tf.reshape(single_label, [])

        if self.mean_loader:
            single_data = self.mean_loader.subtract_mean_op(single_data)
            if LOG_MEAN_FILE:
                self.summaries.append(tf.image_summary('mean_image', tf.expand_dims(self.mean_loader.tf_mean_image, 0), max_images=1))

        # @TODO(tzaman): augmentation here (cropping, etc)
        with tf.name_scope('augment'):
            if self.croplen:
                if self.stage == digits.STAGE_TRAIN:
                    single_data = tf.random_crop(single_data, [self.croplen, self.croplen, self.channels], seed=self._seed)
                else : # Validation or Inference
                    single_data = tf.image.resize_image_with_crop_or_pad(single_data, self.croplen, self.croplen)

        # single_data = tf.image.random_flip_left_right(single_data)
        # single_data = tf.image.random_brightness(single_data, max_delta=50)
        # single_data = tf.image.random_contrast(single_data, lower=0.3, upper=1.6)

        # Subtract off the mean and divide by the variance of the pixels.
        # single_data = tf.image.per_image_whitening(single_data) # converts to float
        
        max_queue_capacity = min(math.ceil(self.total * MIN_FRACTION_OF_EXAMPLES_IN_QUEUE), MAX_ABSOLUTE_EXAMPLES_IN_QUEUE)
        
        single_batch = [single_key, single_data]
        if single_label is not None: # @TODO(tzaman): declare clear 'TRAIN' and 'VAL' and 'INF' descriptors somewhere
            single_batch.append(single_label)

        batch = tf.train.batch(
                single_batch,
                batch_size=self.batch_size,
                dynamic_pad=True, # Allows us to not supply fixed shape a priori
                enqueue_many=False, # Each tensor is a single example
                num_threads=NUM_THREADS_DATA_LOADER,
                capacity=max_queue_capacity, # Max amount that will be loaded and queued
                allow_smaller_final_batch=True, # Happens if total%batch_size!=0
                name='batcher',
            )

        self.batch_k = batch[0] # Key
        self.batch_x = batch[1] # Input
        if len(batch) == 3:
            # There's a label (unlike during inferencing)
            self.batch_y = batch[2] # Output (label)



class LmdbLoader(LoaderFactory):
    def __init__(self):
        pass

    def initialize(self):
        """Sets up the environment for the current data loader

        Args:
            self:

        Returns:
            Nothing.
        """
        # Set up the data loader
        self.lmdb_env = lmdb.open(self.db_path, readonly=True, lock=False)

        self.lmdb_txn = self.lmdb_env.begin(buffers=False)
        self.total = self.lmdb_txn.stat()['entries']
        self.keys = [key for key, _ in self.lmdb_txn.cursor()]

        # Read the first entry to get some info
        lmdb_val = self.lmdb_txn.get(self.keys[0])
        datum = caffe_tf_pb2.Datum()
        datum.ParseFromString(lmdb_val)

        self.channels = datum.channels
        self.width = datum.width
        self.height = datum.height
        self.data_encoded = datum.encoded
        self.float_data = datum.float_data

        if self.data_encoded:
            # Obtain mime-type
            self.data_mime = magic.from_buffer(datum.data, mime=True)

        if not self.float_data:
            if self.bitdepth == 8:
                self.image_dtype = tf.uint8
            else:
                if self.data_mime == 'image/jpeg':
                    logging.error("Tensorflow does not support 16 bit jpeg decoding.")
                    exit(-1)
                self.image_dtype = tf.uint16

    def get_tf_data_type(self):
        """Returns the type of the data, in tf format.
            It takes in account byte-data or floating point data.
            It also takes in account the possible seperate lmdb label db.

        Args:
            self:

        Returns:
            The tensorflow-datatype of the data
        """
        return tf.float32 if self.float_data else tf.string

    def get_tf_label_type(self):
        """Returns the type of the label, in tf format.
            It takes in account byte-data or floating point data.
            It also takes in account the possible seperate lmdb label db.

        Args:
            self:

        Returns:
            The tensorflow-datatype of the label
        """
        if self.labels_db_path:
            return self.labels_db.get_tf_data_type()
        else:
            # No seperate db, return scalar label
            return tf.int64

    def generate_data_op(self):
        """Generates and returns an op that fetches a single sample of data.

        Args:
            self:

        Returns:
            A python function that is inserted as an op
        """
        def get_data_and_shape(lmdb_txn, key):
            val = lmdb_txn.get(key)
            datum = caffe_tf_pb2.Datum()
            datum.ParseFromString(val)
            shape = np.array([datum.width, datum.height, datum.channels], dtype=np.int32)
            if datum.float_data:
                data = np.asarray(datum.float_data, dtype='float32')
            else:
                data = datum.data
            label = np.asarray([datum.label], dtype=np.int64) # scalar label
            return data, shape, label

        def get_data_op(key):
            """Fetches a sample of data and its label from lmdb. If a seperate label database
               exists, it will also load it from the seperate db inside this function. This is
               done the data and its label are loaded at the same time, avoiding multiple queues
               and race conditions.

            Args:
                self: the current lmdb instance

            Returns:
                single_data: One sample of training data
                single_data_shape: The shape of the preceeding training data
                single_label: The label that is the reference value describing the data
                single_label_shape: The shape of the preceeding label data
            """
            single_data, single_data_shape, single_label = get_data_and_shape(self.lmdb_txn, key)
            single_label_shape = np.array([], dtype=np.int32)
            if self.labels_db_path:
                single_label, single_label_shape, _ = get_data_and_shape(self.labels_db.lmdb_txn, key)
            return single_data, [single_data_shape], single_label, [single_label_shape]
        return get_data_op

    def get_single_data(self, key_queue):
        """
        Returns:
            key, single_data, single_data_shape, single_label, single_label_shape
        """
        key = key_queue.dequeue() #Operation that dequeues one key and returns a string with the key
        py_func_return_type = [self.get_tf_data_type(), tf.int32, self.get_tf_label_type(), tf.int32]
        d, ds, l, ls = tf.py_func(self.generate_data_op(), [key], py_func_return_type, name='data_reader')
        return key, d, ds, l, ls

    def __del__(self):
        # Destructor
        self.lmdb_env.close()


class FileListLoader(LoaderFactory):
    """ The FileListLoader loads files from a list of string(s) pointing to (a) file(s).
    These files are then retrieved by their string and loaded according to their extension.
    """
    def __init__(self):
        pass

    def initialize(self):

        self.float_data = False
        self.data_encoded = True

        if self.backend is 'file':
            # Single file
            self.total = 1
            self.keys = [self.db_path]
            first_file_path = self.db_path
        elif self.backend is 'filelist':
            # Single file with a list of files
            with open(self.db_path) as f:
                self.keys = f.readlines()

            # Retain only the images in the list
            self.keys = [key.split(LIST_DELIMITER)[0].rstrip() for key in self.keys]

            if len(self.keys) > 0:
                # Assume the first entry in the line is a pointer to the file path
                first_file_path = self.keys[0]
            else :
                logging.error('Filelist (%s) contains no lines.' % (self.db_path))
                exit(-1)
        else:
            logging.error('Unsupported backend in FileListLoader (%s)' % (self.backend))
            exit(-1)

        self.total = len(self.keys)

        # Check first file for statistics
        im = Image.open(first_file_path)
        self.width, self.height = im.size
        self.channels =  1 if im.mode == 'L' else 3 # @TODO(tzaman): allow more channels

        self.data_mime = magic.from_file(first_file_path, mime=True)

        if self.bitdepth == 8:
            self.image_dtype = tf.uint8
        else:
            if self.data_mime == 'image/jpeg':
                logging.error("Tensorflow does not support 16 bit jpeg decoding.")
                exit(-1)
            self.image_dtype = tf.uint16

        self.reader = tf.WholeFileReader()

    def get_single_data(self, key_queue):
        """
        Returns:
            key, single_data, single_data_shape, single_label, single_label_shape
        """

        key, value = self.reader.read(key_queue)
        shape = np.array([self.width, self.height, self.channels], dtype=np.int32) # @TODO: this is not dynamic
        return key, value, shape


class Hdf5Loader(LoaderFactory):

    def __init__(self):
        pass

    def initialize(self):
        logging.error("NotImplementedError: Hdf5.")
        exit(-1)