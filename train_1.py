""" NYU Natural Language Understanding 2016. Team Grumpy project.

Based on tensorflow's ptb_word_lm.py.

There are 3 supported model configurations: small, medium, large

The hyperparameters used in the model:
- init_scale - the initial scale of the weights
- learning_rate - the initial value of the learning rate
- max_grad_norm - the maximum permissible norm of the gradient
- num_layers - the number of LSTM layers
- num_steps - the number of unrolled steps of LSTM
- hidden_size - the number of LSTM units
- max_epoch - the number of epochs trained with the initial learning rate
- max_max_epoch - the total number of epochs for training
- keep_prob - the probability of keeping weights in the dropout layer
- lr_decay - the decay of the learning rate for each epoch after "max_epoch"
- batch_size - the batch size

The data required for this example is in the data/ dir of the
PTB dataset from Tomas Mikolov's webpage:

$ wget http://www.fit.vutbr.cz/~imikolov/rnnlm/simple-examples.tgz
$ tar xvf simple-examples.tgz

To run:

$ python train.py --data_path=simple-examples/data/

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time

import numpy as np
import tensorflow as tf
import reader_1
import time

import hm_rnn


flags = tf.flags
logging = tf.logging

flags.DEFINE_string(
    "model", "small",
    "A type of model. Possible options are: small, medium, large.")
flags.DEFINE_string("data_path", None,
                    "Where the training/test data is stored.")
flags.DEFINE_string("save_path", None,
                    "Model output directory.")
flags.DEFINE_bool("use_fp16", False,
                  "Train using 16-bit floats instead of 32bit floats")
flags.DEFINE_bool("use_gru", False, "If True, GRU is used. Otherwise, LSTM is used")
flags.DEFINE_bool("use_hm", True, "If set, the hierarchical multiscale version is used."
                  " Otherwise the regular RNN version is used.")
flags.DEFINE_bool("use_dropout", True, "If set, it uses dropout")
flags.DEFINE_integer("max_max_epoch", None, "If set, override the config's max_max_epoch value.")
flags.DEFINE_integer("num_steps", None, "If set, override the config's num_steps value.")

FLAGS = flags.FLAGS


def data_type():
  return tf.float16 if FLAGS.use_fp16 else tf.float32


class PTBInput(object):
  """The input data."""

  def __init__(self, config, data, name=None):
    self.batch_size = batch_size = config.batch_size
    self.num_steps = num_steps = config.num_steps
    self.epoch_size = ((len(data) // batch_size) - 1) // num_steps
    self.input_data, self.targets = reader_1.ptb_producer(
        data, batch_size, num_steps, name=name)


class PTBModel(object):
  """The PTB model."""

  def __init__(self, is_training, config, input_):
    self._input = input_

    batch_size = input_.batch_size
    num_steps = input_.num_steps
    size = config.hidden_size
    vocab_size = config.vocab_size

    if FLAGS.use_hm:
      if FLAGS.use_gru:
        rnn_cell = hm_rnn.HmGruCell(size)
      else:
        rnn_cell = hm_rnn.HmLstmCell(size)
      if is_training and FLAGS.use_dropout and config.keep_prob < 1:
        rnn_cell = tf.nn.rnn_cell.DropoutWrapper(rnn_cell, output_keep_prob=config.keep_prob)
      cell = hm_rnn.MultiHmRNNCell([rnn_cell] * config.num_layers, size)
    else:
      if FLAGS.use_gru:
        rnn_cell = tf.nn.rnn_cell.GRUCell(size)
      else:
        rnn_cell = tf.nn.rnn_cell.BasicLSTMCell(size, forget_bias=0.0, state_is_tuple=True)
      if is_training and FLAGS.use_dropout and config.keep_prob < 1:
        rnn_cell = tf.nn.rnn_cell.DropoutWrapper(rnn_cell, output_keep_prob=config.keep_prob)
      cell = tf.nn.rnn_cell.MultiRNNCell([rnn_cell] * config.num_layers, state_is_tuple=True)
    
    self._initial_state = cell.zero_state(batch_size, data_type())

    with tf.device("/cpu:0"):
      self._embedding = tf.get_variable(
          "embedding", [vocab_size, size], dtype=data_type())
      inputs = tf.nn.embedding_lookup(self._embedding, input_.input_data)

    if is_training and FLAGS.use_dropout and config.keep_prob < 1:
      inputs = tf.nn.dropout(inputs, config.keep_prob)

    outputs = []
    state = self._initial_state
    with tf.variable_scope("RNN"):
      for time_step in range(num_steps):
        if time_step > 0: tf.get_variable_scope().reuse_variables()
        (cell_output, state) = cell(inputs[:, time_step, :], state)
        outputs.append(cell_output)

    output = tf.reshape(tf.concat(1, outputs), [-1, size])
    softmax_w = tf.get_variable(
        "softmax_w", [size, vocab_size], dtype=data_type())
    softmax_b = tf.get_variable("softmax_b", [vocab_size], dtype=data_type())
    logits = tf.matmul(output, softmax_w) + softmax_b

    self._output_probs = tf.nn.softmax(logits)

    loss = tf.nn.seq2seq.sequence_loss_by_example(
        [logits],
        [tf.reshape(input_.targets, [-1])],
        [tf.ones([batch_size * num_steps], dtype=data_type())])
    self._cost = cost = tf.reduce_sum(loss) / batch_size
    self._final_state = state

    if not is_training:
      return

    self._lr = tf.Variable(0.0, trainable=False)
    tvars = tf.trainable_variables()
    print('Trainable variables:')
    print([var.name for var in tvars])
    grads, _ = tf.clip_by_global_norm(tf.gradients(cost, tvars),
                                      config.max_grad_norm)
    optimizer = tf.train.GradientDescentOptimizer(self._lr)
    self._train_op = optimizer.apply_gradients(
        zip(grads, tvars),
        global_step=tf.contrib.framework.get_or_create_global_step())

    self._new_lr = tf.placeholder(
        tf.float32, shape=[], name="new_learning_rate")
    self._lr_update = tf.assign(self._lr, self._new_lr)

  def assign_lr(self, session, lr_value):
    session.run(self._lr_update, feed_dict={self._new_lr: lr_value})

  @property
  def input(self):
    return self._input

  @property
  def initial_state(self):
    return self._initial_state
  
  @property
  def output_probs(self):
    return self._output_probs

  @property
  def cost(self):
    return self._cost

  @property
  def final_state(self):
    return self._final_state

  @property
  def lr(self):
    return self._lr

  @property
  def train_op(self):
    return self._train_op

  @property
  def embedding(self):
    return self._embedding
  

class SmallConfig(object):
  """Small config."""
  init_scale = 0.1
#TODO(GRU):  learning_rate = 1.0
  learning_rate = 0.7 # TODO
  max_grad_norm = 5
  num_layers = 3
  num_steps = 20
  hidden_size = 256
  max_epoch = 4
#  max_max_epoch = 13
  max_max_epoch = 6 # TODO
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000

class SmallGenConfig(object):
  """Small config. for generation"""
  init_scale = 0.1
  learning_rate = 0.7
  max_grad_norm = 5
  num_layers = 2
  num_steps = 1 # this is the main difference
  hidden_size = 200
  max_epoch = 5
  max_max_epoch = 20
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 1
  vocab_size = 10000

class SmallGRUConfig(object):
  """Small config for GRU."""
  init_scale = 0.1
  learning_rate = 0.7 # TODO was 1.0
  max_grad_norm = 5
  num_layers = 3
  num_steps = 20
  hidden_size = 256
  max_epoch = 4
  max_max_epoch = 6
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000


class MediumConfig(object):
  """Medium config."""
  init_scale = 0.05
  learning_rate = 1.0
  max_grad_norm = 5
  num_layers = 3
  num_steps = 35
  hidden_size = 512
  max_epoch = 6
  max_max_epoch = 39
  keep_prob = 0.5
  lr_decay = 0.8
  batch_size = 20
  vocab_size = 10000

class MediumGRUConfig(object):
  """Medium config."""
  init_scale = 0.05
  learning_rate = 0.7  # was 1.0. 0.7 works better.
  max_grad_norm = 5
  num_layers = 3
  num_steps = 35
  hidden_size = 512
  max_epoch = 6
  max_max_epoch = 39
  keep_prob = 0.5
  lr_decay = 0.8
  batch_size = 20
  vocab_size = 10000


class LargeConfig(object):
  """Large config."""
  init_scale = 0.04
  learning_rate = 1.0
  max_grad_norm = 10
  num_layers = 3
  num_steps = 35
  hidden_size = 1024
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 20
  vocab_size = 10000

class LargeGRUConfig(object):
  """Large config."""
  init_scale = 0.04
  learning_rate = 0.7 # was 1.0. 0.7 works better.
  max_grad_norm = 10
  num_layers = 3
  num_steps = 35
  hidden_size = 1024
  max_epoch = 14
  max_max_epoch = 55
  keep_prob = 0.35
  lr_decay = 1 / 1.15
  batch_size = 20
  vocab_size = 10000

class TestConfig(object):
  """Tiny config, for testing."""
  init_scale = 0.1
  learning_rate = 1.0
  max_grad_norm = 1
  num_layers = 1
  num_steps = 2
  hidden_size = 2
  max_epoch = 1
  max_max_epoch = 1
  keep_prob = 1.0
  lr_decay = 0.5
  batch_size = 20
  vocab_size = 10000

  
def run_epoch(session, model, eval_op=None, verbose=False):
  """Runs the model on the given data."""
  start_time = time.time()
  costs = 0.0
  iters = 0
  state = session.run(model.initial_state)

  fetches = {
      "cost": model.cost,
      "final_state": model.final_state,
  }
  if eval_op is not None:
    fetches["eval_op"] = eval_op

  # This sets up the initial set of stacked cells.
  for step in range(model.input.epoch_size):
    feed_dict = {}
    if FLAGS.use_hm:
      if FLAGS.use_gru:
        for i, (h, z) in enumerate(model.initial_state):
          feed_dict[h] = state[i].h
          feed_dict[z] = state[i].z
      else:
        for i, (c, h, z) in enumerate(model.initial_state):
          feed_dict[c] = state[i].c
          feed_dict[h] = state[i].h
          feed_dict[z] = state[i].z
    else:
      if FLAGS.use_gru:
        for i, h in enumerate(model.initial_state):
          feed_dict[h] = state[i]
      else:
        for i, (c, h) in enumerate(model.initial_state):
          feed_dict[c] = state[i].c
          feed_dict[h] = state[i].h
    
    vals = session.run(fetches, feed_dict)
    cost = vals["cost"]
    state = vals["final_state"]

    costs += cost
    iters += model.input.num_steps

    if verbose and step % (model.input.epoch_size // 10) == 10:
      print("%.3f perplexity: %.3f speed: %.0f wps" %
            (step * 1.0 / model.input.epoch_size, np.exp(costs / iters),
             iters * model.input.batch_size / (time.time() - start_time)))

  return np.exp(costs / iters)




def generate_output(session, model, verbose=False):
  """Runs the model on the given data."""
  start_time = time.time()
  costs = 0.0
  iters = 0
  state = session.run(model.initial_state)

  # This sets up the initial set of stacked cells.

  words = ['<start>']
  NUM_WORDS_TO_GENERATE = 360
  for i in range(NUM_WORDS_TO_GENERATE):
    feed_dict = {}
    # TODO get the last element of words
    word = words[-1]
    # TODO convert this to an id with word2id
    x = word2id[word]
    # TODO make word_matrix containing the id
    word_matrix = np.matrix([[x]])  # a 2D numpy matrix 
    feed_dict[model.input_data] = word_matrix
    
    if FLAGS.use_hm:
      if FLAGS.use_gru:
        for i, (h, z) in enumerate(model.initial_state):
          feed_dict[h] = state[i].h
          feed_dict[z] = state[i].z
      else:
        for i, (c, h, z) in enumerate(model.initial_state):
          feed_dict[c] = state[i].c
          feed_dict[h] = state[i].h
          feed_dict[z] = state[i].z
    else:
      if FLAGS.use_gru:
        for i, h in enumerate(model.initial_state):
          feed_dict[h] = state[i]
      else:
        for i, (c, h) in enumerate(model.initial_state):
          feed_dict[c] = state[i].c
          feed_dict[h] = state[i].h

    output_probs, state = session.run([model.output_probs, model.final_state], 
                                      feed_dict)

    # TODO use output probs to get the word index
    id_ = np.argmax(output_probs)
    #unsure what this outputs

    # TODO use index2word to get the word
    output_word =  id_2_word [id_]

    # add the word to the 'words' list

    words.append(output_word)

  print (words)






def get_config():
  config = None
  if FLAGS.model == "small":
    if FLAGS.use_gru:
      config = SmallGRUConfig()
    else:
      config = SmallConfig()
  elif FLAGS.model == "medium":
    if FLAGS.use_gru:
      config = MediumGRUConfig()
    else:
      config = MediumConfig()
  elif FLAGS.model == "large":
    if FLAGS.use_gru:
      config = LargeGRUConfig()
    else:
      config = LargeConfig()
  elif FLAGS.model == "test":
    config = TestConfig()
  else:
    raise ValueError("Invalid model: %s", FLAGS.model)
  if FLAGS.max_max_epoch is not None:
    print('Setting max_max_epoch to flag value of {}'.format(FLAGS.max_max_epoch))
    config.max_max_epoch = FLAGS.max_max_epoch
  if FLAGS.num_steps:
    print('Setting the number of timesteps (num_steps) to flag value of {}'.format(FLAGS.num_steps))
    config.num_steps = FLAGS.num_steps
  return config

  
def main(_):
  if not FLAGS.data_path:
    raise ValueError("Must set --data_path to PTB data directory")

  if FLAGS.use_gru:
    print('Using GRU')
  else:
    print('using LSTM')
  if FLAGS.use_hm:
    print('Using the hierarchical multiscale version of the model')
  else:
    print('Using the non-hierarchical multiscale version of the model')
    
  raw_data = reader_1.ptb_raw_data(FLAGS.data_path)
  train_data, valid_data, test_data, word_to_id, id_2_word = raw_data

  config = get_config()
  if not FLAGS.use_dropout:
    print('Not using dropout')
    config.keep_prob = 1.0
  else:
    print('using dropout')
  eval_config = get_config()
  eval_config.batch_size = 1
  eval_config.num_steps = 1
  
  with tf.Graph().as_default():
    initializer = tf.random_uniform_initializer(-config.init_scale,
                                                config.init_scale)
    print('Building training model...')
    with tf.name_scope("Train"):
      train_input = PTBInput(config=config, data=train_data, name="TrainInput")
      with tf.variable_scope("Model", reuse=None, initializer=initializer):
        m = PTBModel(is_training=True, config=config, input_=train_input)
      tf.scalar_summary("Training Loss", m.cost)
      tf.scalar_summary("Learning Rate", m.lr)

    print('Building validation model...')
    with tf.name_scope("Valid"):
      valid_input = PTBInput(config=config, data=valid_data, name="ValidInput")
      with tf.variable_scope("Model", reuse=True, initializer=initializer):
        mvalid = PTBModel(is_training=False, config=config, input_=valid_input)
      tf.scalar_summary("Validation Loss", mvalid.cost)

    print('Building testing model...')
    with tf.name_scope("Test"):
      test_input = PTBInput(config=config, data=test_data, name="TestInput")
      with tf.variable_scope("Model", reuse=True, initializer=initializer):
        mtest = PTBModel(is_training=False, config=eval_config,
                         input_=test_input)      
  

    sv = tf.train.Supervisor(logdir=FLAGS.save_path)
    
    with sv.managed_session() as session:      
      start_time = time.time()
      for i in range(config.max_max_epoch):
        lr_decay = config.lr_decay ** max(i - config.max_epoch, 0.0)
        m.assign_lr(session, config.learning_rate * lr_decay)

        print("Epoch: %d Learning rate: %.3f" % (i + 1, session.run(m.lr)))
        epoch_start_time = time.time()
        train_perplexity = run_epoch(session, m, eval_op=m.train_op,
                                     verbose=True)
        epoch_end_time = time.time()
        print("Epoch: %d Train Perplexity: %.3f" % (i + 1, train_perplexity))
        print("Epoch: %d Train Time: %.3f minutes" % (i + 1, (epoch_end_time-epoch_start_time)/60))
        valid_perplexity = run_epoch(session, mvalid)
        print("Epoch: %d Valid Perplexity: %.3f" % (i + 1, valid_perplexity))

      print("Training complete! Total time: %.3f hours" % ((time.time()-start_time)/3600))
      test_perplexity = run_epoch(session, mtest)
      print("Test Perplexity: %.3f" % test_perplexity)

      test_perplexity = generate_output(session, mtest)
      print("Test Perplexity: %.3f" % test_perplexity)

      if FLAGS.save_path:
        print("Saving model to %s." % FLAGS.save_path)
        sv.saver.save(session, FLAGS.save_path, global_step=sv.global_step)

if __name__ == "__main__":
  tf.app.run()
