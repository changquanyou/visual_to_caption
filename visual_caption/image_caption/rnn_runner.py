# -*- coding:utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals  # compatible with python3 unicode coding

import math
import os
import time

import numpy as np
import tensorflow as tf
from tensorflow.contrib.learn import ModeKeys

from visual_caption.base.base_runner import BaseRunner
from visual_caption.image_caption.data.data_config import ImageCaptionDataConfig
from visual_caption.image_caption.data.data_reader import ImageCaptionDataReader
from visual_caption.image_caption.data.data_utils import ImageCaptionDataUtils
from visual_caption.image_caption.feature.feature_extractor import FeatureExtractor
from visual_caption.image_caption.model.image_caption_config import ImageCaptionConfig
from visual_caption.image_caption.model.image_rnn_model import ImageRNNModel
from visual_caption.utils.decorator_utils import timeit


class RNNCaptionRunner(BaseRunner):
    def __init__(self):
        super(RNNCaptionRunner, self).__init__()

        self.data_config = ImageCaptionDataConfig()
        self.data_reader = ImageCaptionDataReader(data_config=self.data_config)
        self.model_config = ImageCaptionConfig(data_config=self.data_config,
                                               model_name=self.data_config.model_name)

        self.index2token = self.data_reader.data_embedding.index2token
        self.token2index = self.data_reader.data_embedding.token2index

        self.token_begin = self.model_config.data_config.token_begin
        self.token_end = self.model_config.data_config.token_end

        self.token_begin_id = self.token2index[self.token_begin]
        self.token_end_id = self.token2index[self.token_end]

        # self.feature_manager = None

        pass

    @timeit
    def train(self):
        model = ImageRNNModel(model_config=self.model_config,
                              data_reader=self.data_reader,
                              mode=ModeKeys.TRAIN)
        fetches = [model.summary_merged, model.loss, model.accuracy, model.train_op,
                   model.image_ids, model.input_seqs, model.target_seqs, model.predictions]

        format_string = "{0}: epoch={1:2d}, batch={2:6d}, batch_size={3:2d}, " \
                        "step={4:6d}, loss={5:.6f}, acc={6:.6f}, elapsed={7:.6f}"
        with tf.Session(config=self.model_config.sess_config) as sess:
            model.summary_writer.add_graph(sess.graph)
            if not model.restore_model(sess=sess):
                model.logger.info("Created model with fresh parameters.")
                init_op = tf.group(tf.local_variables_initializer(),
                                   tf.global_variables_initializer())
                sess.run(init_op)
                sess.run(tf.tables_initializer())
                max_acc = 0.0
            else:
                # running first internal evaluation
                sess.run(tf.tables_initializer())
                max_acc = self._internal_eval(model=model, sess=sess)

            train_init_op = self.data_reader.get_train_init_op()
            begin = time.time()

            for epoch in range(model.model_config.max_max_epoch):
                sess.run(train_init_op)  # initial train data options
                step_begin = time.time()
                batch = 0
                while True:  # train each batch in a epoch
                    try:
                        result_batch = sess.run(fetches)  # run training step
                        batch += 1
                        global_step = tf.train.global_step(sess, model.global_step_tensor)
                        # display and summarize training result
                        if batch % model.model_config.display_and_summary_step == 0:
                            batch_summary, loss, acc, _, image_ids, \
                            input_seqs, target_seqs, predicts = result_batch
                            batch_size = len(predicts)
                            # self._display_content(image_ids, input_seqs, predicts, target_seqs)
                            # add train summaries
                            model.summary_writer.add_summary(
                                summary=batch_summary, global_step=global_step)
                            print(format_string.format(model.mode, epoch, batch, batch_size,
                                                       global_step, loss, acc, time.time() - step_begin))
                            step_begin = time.time()
                    except tf.errors.OutOfRangeError:  # ==> "End of training dataset"
                        try:
                            valid_result = self._internal_eval(model=model, sess=sess)
                        except tf.errors.OutOfRangeError:
                            global_step = tf.train.global_step(sess,
                                                               model.global_step_tensor)
                            model.logger.info("finished validation in training step {}"
                                              .format(global_step))
                        valid_acc = valid_result
                        if valid_acc > max_acc:  # save the best model session
                            max_acc = valid_acc
                            model.save_model(sess=sess, global_step=global_step)
                            print('training: epoch={}, step={}, validation: average_result ={}'
                                  .format(epoch, global_step, valid_result))
                        print("training epoch={} finished with {} batches, global_step={}, elapsed={} "
                              .format(epoch, batch, global_step, time.time() - begin))
                        break  # break the training while True
        pass
        # validation with current (such as training) session on validation data set

    def _display_content(self, image_ids, input_seqs, predicts, target_seqs):

        for idx, image_id in enumerate(image_ids):
            if idx % 10 == 0:
                input_seq = input_seqs[idx]
                caption_byte_list = [self.index2token[token_id] for token_id in input_seq]
                caption_text = ' '.join(caption_byte_list)

                target = target_seqs[idx].tolist()
                target_byte_list = [self.index2token[token_id]
                                    for idx, token_id in enumerate(target)
                                    # if idx < target.index(self.token_end_id)
                                    ]
                target_text = ' '.join(target_byte_list)

                predict = predicts[idx].tolist()
                predict_byte_list = [self.index2token[token_id]
                                     for idx, token_id in enumerate(predict)
                                     # if idx < predict.index(self.token_end_id)
                                     ]

                predict_text = ' '.join(predict_byte_list)

                print("image_id={}, \n\tcaption=[{}]\n\ttarget= [{}]\n\tpredict=[{}]"
                      .format(image_ids[idx], caption_text, target_text, predict_text))

    @timeit
    def _internal_eval(self, model, sess):
        """
        running internal evaluation with current sess
        :param model:
        :param sess:
        :return:
        """
        fetches = [model.accuracy, model.target_cross_entropy_losses, model.summary_merged, model.predictions]
        batch_count = 0
        eval_acc = 0.0
        validation_init_op = self.data_reader.get_valid_init_op()
        # initialize validation dataset
        sess.run(validation_init_op)
        step_begin = time.time()
        global_step = tf.train.global_step(sess, model.global_step_tensor)
        sum_losses = 0.
        start_time = time.time()
        while True:  # iterate eval batch at step
            try:
                eval_step_result = sess.run(fetches=fetches)
                acc, eval_loss, summaries, predictions = eval_step_result
                sum_losses += np.sum(eval_loss)
                eval_acc += acc
                batch_count += 1
                if batch_count % self.model_config.display_and_summary_step == 0:
                    print("valid: step={0:8d}, batch={1} loss={2:.4f}, acc={3:.4f}, elapsed={4:.4f}"
                          .format(global_step, batch_count, eval_loss, acc, time.time() - step_begin))
                step_begin = time.time()
                if batch_count >= 300:
                    break
            except tf.errors.OutOfRangeError:  # ==> "End of validation dataset"
                print("validation finished : step={0}, batch={1}, elapsed={2:.4f}"
                      .format(global_step, batch_count, time.time() - step_begin))
                break

        if batch_count > 0:
            # Log perplexity to the FileWriter.
            perplexity = math.exp(sum_losses)
            eval_time = time.time() - start_time
            tf.logging.info("Perplexity = %f (%.2g sec)", perplexity, eval_time)
            summary = tf.Summary()
            value = summary.value.add()
            value.simple_value = perplexity
            value.tag = "Perplexity"
            model.summary_eval_writer.add_summary(summary, global_step)

            eval_acc = eval_acc / batch_count

        result = eval_acc
        return result
        pass

    @timeit
    def valid(self):
        pass

    @timeit
    def eval(self):
        model = ImageRNNModel(model_config=self.model_config,
                              data_reader=self.data_reader,
                              mode=ModeKeys.EVAL)
        fetches = [model.loss, model.accuracy,
                   model.image_ids, model.input_seqs, model.target_seqs,
                   model.predictions]
        format_string = "{}: batch={:6d}, step={:6d}, loss={:.6f}, acc={:.6f}, elapsed={:.6f}"
        with tf.Session(config=model.model_config.sess_config) as sess:
            model.summary_writer.add_graph(sess.graph)
            # CheckPoint State
            if not model.restore_model(sess=sess):
                init_op = tf.group(tf.local_variables_initializer(),
                                   tf.global_variables_initializer())
                sess.run(init_op)

            sess.run(tf.tables_initializer())
            begin = time.time()
            infer_init_op = model.data_reader.get_valid_init_op()
            sess.run(infer_init_op)  # initial infer data options
            batch = 0
            global_step = tf.train.global_step(sess, model.global_step_tensor)
            while True:  # train each batch in a epoch
                try:
                    batch_data = sess.run(model.next_batch)
                    (image_ids, image_features, captions, targets, caption_ids, target_ids,
                     caption_lengths, target_lengths) = batch_data
                    feed_dict = {

                        model.image_ids: image_ids,
                        model.image_feature: image_features,

                        model.input_seqs: caption_ids,
                        model.target_seqs: target_ids,

                        model.input_lengths: caption_lengths,
                        model.target_lengths: target_lengths,

                    }
                    result_batch = sess.run(fetches=fetches, feed_dict=feed_dict)
                    batch += 1
                    global_step = tf.train.global_step(sess, model.global_step_tensor)
                    loss, acc, image_ids, input_seqs, target_seqs, predicts = result_batch
                    self._display_content(image_ids=image_ids,
                                          input_seqs=input_seqs,
                                          target_seqs=target_seqs,
                                          predicts=predicts)
                    step_begin = time.time()
                except tf.errors.OutOfRangeError:  # ==> "End of training dataset"
                    print(" finished with {} batches, global_step={}, elapsed={} "
                          .format(batch, global_step, time.time() - begin))
                    break  # break the training while True

        pass

    @timeit
    def _internal_infer(self, model, sess, image_features):
        # get initial state for  infer_model
        batch_size = len(image_features)
        feed_dict = {model.image_features: image_features}
        initial_states = sess.run(fetches=model.initial_states, feed_dict=feed_dict)

        # the first step inference
        infer_fetches = [model.predictions, model.final_states]
        begin_inputs = [self.token_begin_id for _ in range(batch_size)]

        feed_dict = {model.image_features: image_features,
                     model.input_feeds: begin_inputs,
                     model.state_feeds: initial_states}
        predict_ids, new_states = sess.run(fetches=infer_fetches, feed_dict=feed_dict)

        caption_ids = [[id] for id in predict_ids]
        for i in range(model.max_seq_length):  # for each inference step
            # feed_dict for next step
            feed_dict = {model.image_features: image_features,
                         model.input_feeds: predict_ids,
                         model.state_feeds: new_states}

            predict_ids, new_states = sess.run(fetches=infer_fetches, feed_dict=feed_dict)
            for idx, predict_id in enumerate(predict_ids):
                caption_ids[idx].append(predict_id)
        return caption_ids

    def infer(self):
        # feature_gen = self.get_test_images()
        model = ImageRNNModel(model_config=self.model_config,
                              data_reader=self.data_reader,
                              mode=ModeKeys.INFER)
        # use train data as infer data
        data_init_op = self.data_reader.get_valid_init_op()
        with tf.Session(config=model.model_config.sess_config) as sess:
            model.restore_model(sess=sess)
            sess.run(tf.tables_initializer())
            sess.run(data_init_op)
            global_step = tf.train.global_step(sess, model.global_step_tensor)
            while True:  # train each batch in a epoch
                try:
                    infer_batch_data = sess.run(model.next_batch)
                    (image_ids, image_features, captions, targets,
                     caption_ids, target_ids, caption_lengths, target_lengths) = infer_batch_data
                    predict_caption_ids = self._internal_infer(model=model, sess=sess,
                                                               image_features=image_features)
                    for idx, predict_ids in enumerate(predict_caption_ids):
                        caption = [self.index2token[id] for id in predict_ids]
                        print("caption={}".format(caption))
                except tf.errors.OutOfRangeError:
                    print("Infer finished at global_step={0}".format(global_step))
                    break  # break the training while True

    def get_test_images(self):
        feature_manager = FeatureExtractor(sess=None)
        image_filenames = os.listdir(self.data_config.train_image_dir)
        batch_size = 20
        image_batch = list()
        for filename in image_filenames:
            image_file = os.path.join(self.data_config.train_image_dir, filename)
            image_raw = ImageCaptionDataUtils.load_image_raw(image_file=image_file)
            image_batch.append(image_raw)
            if len(image_batch) == batch_size:
                features = feature_manager.get_features(image_batch)
                yield features
                image_batch = []
        if len(image_batch) > 0:
            features = feature_manager.get_features(image_batch)
            yield features
        del image_batch
        pass


def main(_):
    runner = RNNCaptionRunner()
    runner.train()
    # runner.eval()
    runner.infer()


if __name__ == '__main__':
    tf.app.run()