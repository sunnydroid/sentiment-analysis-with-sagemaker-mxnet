# Code based on: https://github.com/awslabs/amazon-sagemaker-examples/blob/master/sagemaker-python-sdk/mxnet_gluon_sentiment/sentiment.py

from __future__ import print_function

import logging
import mxnet as mx
from mxnet import gluon, autograd, nd
from mxnet.gluon import nn
import numpy as np
import json
import time
import re
from mxnet.io import DataIter, DataBatch, DataDesc
import bisect, random
from collections import Counter
from itertools import chain, islice


logging.basicConfig(level=logging.DEBUG)

# ------------------------------------------------------------ #
# Training methods                                             #
# ------------------------------------------------------------ #


def train(current_host, hosts, num_cpus, num_gpus, channel_input_dirs, model_dir, hyperparameters, **kwargs):
    """
    Runs Apache MXNet training. Amazon SageMaker calls this function with information
    about the training environment. When called, if this function returns an
    object, that object is passed to a save function.  The save function
    can be used to serialize the model to the Amazon SageMaker training job model
    directory.

    The **kwargs parameter can be used to absorb any Amazon SageMaker parameters that
    your training job doesn't need to use. For example, if your training job
    doesn't need to know anything about the training environment, your function
    signature can be as simple as train(**kwargs).

    Amazon SageMaker invokes your train function with the following python kwargs:

    Args:
        - hyperparameters: The Amazon SageMaker Hyperparameters dictionary. A dict
            of string to string.
        - input_data_config: The Amazon SageMaker input channel configuration for
            this job.
        - channel_input_dirs: A dict of string-to-string maps from the
            Amazon SageMaker algorithm input channel name to the directory containing
            files for that input channel. Note, if the Amazon SageMaker training job
            is run in PIPE mode, this dictionary will be empty.
        - output_data_dir:
            The Amazon SageMaker output data directory. After the function returns, data written to this
            directory is made available in the Amazon SageMaker training job
            output location.
        - model_dir: The Amazon SageMaker model directory. After the function returns, data written to this
            directory is made available to the Amazon SageMaker training job
            model location.
        - num_gpus: The number of GPU devices available on the host this script
            is being executed on.
        - num_cpus: The number of CPU devices available on the host this script
            is being executed on.
        - hosts: A list of hostnames in the Amazon SageMaker training job cluster.
        - current_host: This host's name. It will exist in the hosts list.
        - kwargs: Other keyword args.

    Returns:
        - (object): Optional. An Apache MXNet model to be passed to the model
            save function. If you do not return anything (or return None),
            the save function is not called.
    """
    
    # retrieve the hyperparameters
    batch_size = hyperparameters.get('batch_size', 8)
    epochs = hyperparameters.get('epochs', 2)
    learning_rate = hyperparameters.get('learning_rate', 0.01)
    log_interval = hyperparameters.get('log_interval', 1000)
    embedding_size = hyperparameters.get('embedding_size', 50)

    if len(hosts) == 1:
        kvstore = 'device' if num_gpus > 0 else 'local'
    else:
        kvstore = 'dist_device_sync' if num_gpus > 0 else 'dist_sync'

    ctx = mx.gpu() if num_gpus > 0 else mx.cpu()

    training_dir = channel_input_dirs['training']
    train_sentences, train_labels, _ = get_dataset(training_dir + '/train')
    val_sentences, val_labels, _ = get_dataset(training_dir + '/test')

    num_classes = len(set(train_labels))
    vocab = create_vocab(train_sentences)
    vocab_size = len(vocab)

    train_sentences = [[vocab.get(token, 1) for token in line if len(line)>0] for line in train_sentences]
    val_sentences = [[vocab.get(token, 1) for token in line if len(line)>0] for line in val_sentences]

    # Alternatively to splitting in memory, the data could be pre-split in S3 and use ShardedByS3Key
    # to do parallel training.
    shard_size = len(train_sentences) // len(hosts)
    for i, host in enumerate(hosts):
        if host == current_host:
            start = shard_size * i
            end = start + shard_size
            break

    train_iterator = BucketSentenceIter(train_sentences[start:end], train_labels[start:end], batch_size)
    val_iterator = BucketSentenceIter(val_sentences, val_labels, batch_size)

    # define the Gluon network
    net = TextClassifier(vocab_size, embedding_size, num_classes)

    # Collect all parameters from net and its children, then initialize them.
    net.initialize(mx.init.Xavier(magnitude=2.24), ctx=ctx)
    
    # Trainer updates parameters with the gradient.
    trainer = gluon.Trainer(net.collect_params(), 'adam',
                            {'learning_rate': learning_rate},
                            kvstore=kvstore)
    metric = mx.metric.Accuracy()
    loss = gluon.loss.SoftmaxCrossEntropyLoss()

    # Training loop
    for epoch in range(epochs):
        # reset data iterator and metric at begining of epoch.
        metric.reset()
        btic = time.time()
        i = 0
        for batch in train_iterator:
            # Copy data to ctx if necessary
            data = batch.data[0].as_in_context(ctx)
            label = batch.label[0].as_in_context(ctx)

            # Start recording computation graph with record() section.
            # Recorded graphs can then be differentiated with backward.
            with autograd.record():
                output = net(data)
                L = loss(output, label)
                L.backward()
                
            # take a gradient step with batch_size equal to data.shape[0]
            trainer.step(data.shape[0])
            
            # update metric at last.
            metric.update([label], [output])

            # Log training status
            if i % log_interval == 0 and i > 0:
                name, acc = metric.get()
                print('[Epoch %d Batch %d] Training: %s=%f, %f samples/s' %
                      (epoch, i, name, acc, batch_size / (time.time() - btic)))

            btic = time.time()
            i += 1

        name, acc = metric.get()
        print('[Epoch %d] Training: %s=%f' % (epoch, name, acc))

        name, val_acc = test(ctx, net, val_iterator)
        print('[Epoch %d] Validation: %s=%f' % (epoch, name, val_acc))
        train_iterator.reset()
    return net, vocab


class BucketSentenceIter(DataIter):
    """Simple bucketing iterator for text classification model.
    Parameters
    ----------
    sentences : list of list of int
        Encoded sentences.
    labels : list of int
        Corresponding labels.
    batch_size : int
        Batch size of the data.
    buckets : list of int, optional
        Size of the data buckets. Automatically generated if None.
    invalid_label : int, optional
        Key for invalid label, e.g. <unk. The default is 0.
    dtype : str, optional
        Data type of the encoding. The default data type is 'float32'.
    data_name : str, optional
        Name of the data. The default name is 'data'.
    label_name : str, optional
        Name of the label. The default name is 'softmax_label'.
    layout : str, optional
        Format of data and label. 'NT' means (batch_size, length)
        and 'TN' means (length, batch_size).
    """
    def __init__(self, sentences, labels, batch_size, buckets=None, invalid_label=0,
                 data_name='data', label_name='softmax_label', dtype='float32',
                 layout='NT'):
        super(BucketSentenceIter, self).__init__()
        if not buckets:
            buckets = [i for i, j in enumerate(np.bincount([len(s) for s in sentences]))
                       if j >= batch_size]
        buckets.sort()

        ndiscard = 0
        self.data = [[] for _ in buckets]
        self.labels = [[] for _ in buckets]
        for i, sent in enumerate(sentences):
            buck = bisect.bisect_left(buckets, len(sent))
            if buck == len(buckets):
                ndiscard += 1
                continue
            buff = np.full((buckets[buck],), invalid_label, dtype=dtype)
            buff[:len(sent)] = sent
            self.data[buck].append(buff)
            self.labels[buck].append(labels[i])

        self.data = [np.asarray(i, dtype=dtype) for i in self.data]
        self.labels = [np.asarray(i, dtype=dtype) for i in self.labels]

        print("WARNING: discarded %d sentences longer than the largest bucket."%ndiscard)

        self.batch_size = batch_size
        self.buckets = buckets
        self.data_name = data_name
        self.label_name = label_name
        self.dtype = dtype
        self.invalid_label = invalid_label
        self.nddata = []
        self.ndlabel = []
        self.major_axis = layout.find('N')
        self.layout = layout
        self.default_bucket_key = max(buckets)

        if self.major_axis == 0:
            self.provide_data = [DataDesc(
                name=self.data_name, shape=(batch_size, self.default_bucket_key),
                layout=self.layout)]
            self.provide_label = [DataDesc(
                name=self.label_name, shape=(batch_size,),
                layout=self.layout)]
        elif self.major_axis == 1:
            self.provide_data = [DataDesc(
                name=self.data_name, shape=(self.default_bucket_key, batch_size),
                layout=self.layout)]
            self.provide_label = [DataDesc(
                name=self.label_name, shape=(self.default_bucket_key, batch_size),
                layout=self.layout)]
        else:
            raise ValueError("Invalid layout %s: Must by NT (batch major) or TN (time major)")

        self.idx = []
        for i, buck in enumerate(self.data):
            self.idx.extend([(i, j) for j in range(0, len(buck) - batch_size + 1, batch_size)])
        self.curr_idx = 0
        self.reset()

    def reset(self):
        """Resets the iterator to the beginning of the data."""
        self.curr_idx = 0
        random.shuffle(self.idx)
        for i in range(len(self.data)):
            data, labels =  self.data[i], self.labels[i]
            p = np.random.permutation(len(data))
            self.data[i], self.labels[i] = data[p], labels[p]

        self.nddata = []
        self.ndlabel = []
        for buck,label_buck in zip(self.data, self.labels):
            self.nddata.append(nd.array(buck, dtype=self.dtype))
            self.ndlabel.append(nd.array(label_buck, dtype=self.dtype))

    def next(self):
        """Returns the next batch of data."""
        if self.curr_idx == len(self.idx):
            raise StopIteration
        i, j = self.idx[self.curr_idx]
        self.curr_idx += 1

        if self.major_axis == 1:
            data = self.nddata[i][j:j+self.batch_size].T
            label = self.ndlabel[i][j:j+self.batch_size].T
        else:
            data = self.nddata[i][j:j+self.batch_size]
            label = self.ndlabel[i][j:j+self.batch_size]

        return DataBatch([data], [label], pad=0,
                         bucket_key=self.buckets[i],
                         provide_data=[DataDesc(
                             name=self.data_name, shape=data.shape,
                             layout=self.layout)],
                         provide_label=[DataDesc(
                             name=self.label_name, shape=label.shape,
                             layout=self.layout)])


class TextClassifier(gluon.HybridBlock):
    # Text classification model
    
    def __init__(self, vocab_size, embedding_size, classes, **kwargs):
        super(TextClassifier, self).__init__(**kwargs)
        with self.name_scope():
            self.dense = gluon.nn.Dense(classes)
            self.embedding = gluon.nn.Embedding(input_dim=vocab_size, output_dim=embedding_size)

    def hybrid_forward(self, F, x):
        # Here `F` can be either mx.nd or mx.sym, x is the input data
        x = self.embedding(x)
        x = F.mean(x, axis=1)
        x = self.dense(x)
        return x


def get_dataset(filename):
    labels = []
    sentences = []
    max_length = -1
    with open(filename) as f:
        for line in f:
            tokens = line.split()
            label = int(tokens[0])
            words = tokens[1:]
            max_length = max(max_length, len(words))
            labels.append(label)
            sentences.append(words)
    return sentences, labels, max_length


def create_vocab(sentences, min_count=5, num_words = 100000):
    BOS_SYMBOL = "<s>"
    EOS_SYMBOL = "</s>"
    UNK_SYMBOL = "<unk>"
    PAD_SYMBOL = "<pad>"
    PAD_ID = 0
    TOKEN_SEPARATOR = " "
    VOCAB_SYMBOLS = [PAD_SYMBOL, UNK_SYMBOL, BOS_SYMBOL, EOS_SYMBOL]
    VOCAB_ENCODING = "utf-8"
    vocab_symbols_set = set(VOCAB_SYMBOLS)
    raw_vocab = Counter(token for line in sentences for token in line)
    pruned_vocab = sorted(((c, w) for w, c in raw_vocab.items() if c >= min_count), reverse=True)
    vocab = islice((w for c, w in pruned_vocab), num_words)
    word_to_id = {word: idx for idx, word in enumerate(chain(VOCAB_SYMBOLS, vocab))}
    return word_to_id


def vocab_to_json(vocab, path):
    with open(path, "w") as out:
        json.dump(vocab, out, indent=4, ensure_ascii=True)
        print('Vocabulary saved to "%s"', path)


def vocab_from_json(path):
    with open(path) as inp:
        vocab = json.load(inp)
        print('Vocabulary (%d words) loaded from "%s"', len(vocab), path)
        return vocab


def save(net, model_dir):
    # save the model
    net, vocab = net
    y = net(mx.sym.var('data'))
    y.save('%s/model.json' % model_dir)
    net.collect_params().save('%s/model.params' % model_dir)
    vocab_to_json(vocab, '%s/vocab.json' % model_dir)


def test(ctx, net, val_data):
    val_data.reset()
    metric = mx.metric.Accuracy()
    for batch in val_data:
        data = batch.data[0].as_in_context(ctx)
        label = batch.label[0].as_in_context(ctx)
        output = net(data)
        metric.update([label], [output])
    return metric.get()


# ------------------------------------------------------------ #
# Hosting methods                                              #
# ------------------------------------------------------------ #

def model_fn(model_dir):
    """
    Load the gluon model. Called once when hosting service starts.

    :param: model_dir The directory where model files are stored.
    :return: a model (in this case a Gluon network)
    """
    symbol = mx.sym.load('%s/model.json' % model_dir)
    vocab = vocab_from_json('%s/vocab.json' % model_dir)
    outputs = mx.symbol.softmax(data=symbol, name='softmax_label')
    inputs = mx.sym.var('data')
    param_dict = gluon.ParameterDict('model_')
    net = gluon.SymbolBlock(outputs, inputs, param_dict)
    net.load_params('%s/model.params' % model_dir, ctx=mx.cpu())
    return net, vocab


def transform_fn(net, data, input_content_type, output_content_type):
    """
    Transform a request using the Gluon model. Called once per request.

    :param net: The Gluon model.
    :param data: The request payload.
    :param input_content_type: The request content type.
    :param output_content_type: The (desired) response content type.
    :return: response payload and content type.
    """
    # we can use content types to vary input/output handling, but
    # here we just assume json for both
    net, vocab = net
    parsed = json.loads(data)
    outputs = []
    for row in parsed:
        tokens = [vocab.get(token, 1) for token in row.split()]
        nda = mx.nd.array([tokens])
        output = net(nda)
        prediction = mx.nd.argmax(output, axis=1)
        outputs.append(int(prediction.asscalar()))
    response_body = json.dumps(outputs)
    return response_body, output_content_type
