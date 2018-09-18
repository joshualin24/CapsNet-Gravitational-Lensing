"""CapsNet model.

Related papers:
https://arxiv.org/pdf/1710.09829.pdf
http://www.cs.toronto.edu/~fritz/absps/transauto6.pdf
"""
import numpy as np
import tensorflow as tf
from tf_util import cost_tensor
from model.baseline import Baseline


class CapsNet(Baseline):
    """CapsNet model."""

    def __init__(self, hps, images, labels):
        """CapsNet constructor"""

        """
        Args:
          hps: Hyperparameters.
          images: Batches of images. [batch_size, image_size, image_size, channels]
          labels: Batches of labels. [batch_size, num_classes]
        """
        super().__init__(hps, images, labels)

    def _squash(self, x, axis=-1):
        """https://arxiv.org/pdf/1710.09829.pdf (eq.1)

           squash activation that normalizes vectors by their relative lengths
        """
        square_norm = tf.reduce_sum(tf.square(x), axis, keep_dims=True)
        scale = square_norm / (1 + square_norm) / tf.sqrt(square_norm + 1e-8)
        x = tf.multiply(scale, x)
        return x

    def _capsule_layer(self, x, params_shape, num_routing, name=''):
        """
        Credit XifengGuo: https://github.com/XifengGuo/CapsNet-Keras/blob/master/capsulelayers.py

        Xifeng Guo:
        The capsule layer. It is similar to Dense layer. Dense layer has `in_num` inputs, each is a scalar, the output of the
        neuron from the former layer, and it has `out_num` output neurons. CapsuleLayer just expand the output of the neuron
        from scalar to vector. So its input shape = [None, input_num_capsule, input_dim_capsule] and output shape = \
        [None, num_capsule, dim_capsule]. For Dense Layer, input_dim_capsule = dim_capsule = 1.

        :param num_routing: number of iterations for the routing algorithm
        """
        assert len(params_shape) == 4, "Given wrong parameter shape."
        output_num_capsule, input_num_capsule, output_dim_capsule, input_dim_capsule = params_shape

        x = self._batch_norm(name + 'bn', x)

        W = tf.get_variable(
            name + 'DW', params_shape, tf.float32,
            initializer=tf.contrib.layers.variance_scaling_initializer(factor=1.0, mode='FAN_AVG', uniform=True))

        inputs = x
        # Credit XifengGuo:
        # inputs.shape=[None, input_num_capsule, input_dim_capsule]
        # inputs_expand.shape=[None, 1, input_num_capsule, input_dim_capsule]
        inputs_expand = tf.expand_dims(inputs, 1)
        tf.logging.info('Expanding inputs to be {}'.format(inputs_expand.get_shape()))

        # Credit XifengGuo:
        # Replicate output_num_capsule dimension to prepare being multiplied by W
        # inputs_tiled.shape=[None, output_num_capsule, input_num_capsule, input_dim_capsule]
        inputs_tiled = tf.tile(inputs_expand, [1, output_num_capsule, 1, 1])
        tf.logging.info('Tiling inputs to be {}'.format(inputs_tiled.get_shape()))

        # Credit XifengGuo:
        # Compute `inputs * W` by scanning inputs_tiled on dimension 0.
        # x.shape=[output_num_capsule, input_num_capsule, input_dim_capsule]
        # W.shape=[output_num_capsule, input_num_capsule, output_dim_capsule, input_dim_capsule]
        # Regard the first two dimensions as `batch` dimension,
        # then matmul: [input_dim_capsule] x [output_dim_capsule, input_dim_capsule]^T -> [output_dim_capsule].
        # inputs_hat.shape = [None, output_num_capsule, input_num_capsule, output_dim_capsule]
        inputs_hat = tf.map_fn(lambda x: tf.keras.backend.batch_dot(
            x, W, [2, 3]), elems=inputs_tiled)

        # Credit XifengGuo:
        # Begin: Routing algorithm ---------------------------------------------------------------------#
        # In forward pass, `inputs_hat_stopped` = `inputs_hat`;
        # In backward, no gradient can flow from `inputs_hat_stopped` back to `inputs_hat`.
        inputs_hat_stopped = tf.stop_gradient(inputs_hat)

        # Credit XifengGuo:
        # The prior for coupling coefficient, initialized as zeros.
        # b.shape = [None, self.output_num_capsule, self.input_num_capsule].
        b = tf.zeros(shape=[tf.shape(inputs_hat)[0],
                            output_num_capsule, input_num_capsule])

        assert num_routing > 0, 'The num_routing should be > 0.'
        for i in range(num_routing):
            # Credit XifengGuo:
            # c.shape=[batch_size, output_num_capsule, input_num_capsule]
            c = tf.nn.softmax(b, dim=1)

            # Credit XifengGuo:
            # At last iteration, use `inputs_hat` to compute `outputs` in order to backpropagate gradient
            if i == num_routing - 1:
                # Credit XifengGuo:
                # c.shape =  [batch_size, output_num_capsule, input_num_capsule]
                # inputs_hat.shape=[None, output_num_capsule, input_num_capsule, output_dim_capsule]
                # The first two dimensions as `batch` dimension,
                # then matmal: [input_num_capsule] x [input_num_capsule, output_dim_capsule] -> [output_dim_capsule].
                # outputs.shape=[None, output_num_capsule, output_dim_capsule]
                outputs = self._squash(tf.keras.backend.batch_dot(
                    c, inputs_hat, [2, 2]))  # [None, 10, 16]
            else:  # Otherwise, use `inputs_hat_stopped` to update `b`. No gradients flow on this path.
                outputs = self._squash(tf.keras.backend.batch_dot(
                    c, inputs_hat_stopped, [2, 2]))
                # Credit XifengGuo:
                # outputs.shape =  [None, output_num_capsule, output_dim_capsule]
                # inputs_hat.shape=[None, output_num_capsule, input_num_capsule, output_dim_capsule]
                # The first two dimensions as `batch` dimension,
                # then matmal: [output_dim_capsule] x [input_num_capsule, output_dim_capsule]^T -> [input_num_capsule].
                # b.shape=[batch_size, output_num_capsule, input_num_capsule]
                b += tf.keras.backend.batch_dot(outputs, inputs_hat_stopped, [2, 3])
        # Credit XifengGuo:
        # End: Routing algorithm -----------------------------------------------------------------------#
        tf.logging.info('image after this capsule layer {}'.format(outputs.get_shape()))
        return outputs

    def build_graph(self):
        """Build a whole graph for the model."""

        """
            1. create anxilliary parameters
            2. create CapsNet core layers
            3. create trainer
        """

        self._build_init()
        self.cost = self._build_model()

        grads_vars = self.optimizer.compute_gradients(self.cost)

        self._build_train_op(grads_vars)
        self.summaries = tf.summary.merge_all()

    """Overrride _build_model"""

    def _build_model(self):
        """Build the core model within the graph."""

        filters = self.hps.filters
        assert isinstance(filters, list)
        strides = self.hps.strides
        assert isinstance(strides, list)
        cnn_kernel_size = self.hps.cnn_kernel_size
        assert isinstance(cnn_kernel_size, int)
        padding = self.hps.padding

        with tf.variable_scope('init'):
            x = self.images
            x = self._batch_norm('init_bn', x)
            x = self._conv('init_conv', x, cnn_kernel_size,
                           filters[0], filters[1], self._stride_arr(strides[0]), padding=padding)
            x = self._relu(x)
            tf.logging.info('image after init {}'.format(x.get_shape()))

        with tf.variable_scope('primal_capsules'):
            x = self._batch_norm('primal_capsules_bn', x)
            x = self._conv('primal_capsules_conv', x, cnn_kernel_size, filters[1],
                           filters[1], self._stride_arr(strides[1]), padding=padding)
            #x = self._squash(x)
            capsules_dims = filters[2]
            num_capsules = np.prod(x.get_shape().as_list()[1:]) // capsules_dims
            # TensorFlow does the trick
            x = tf.reshape(x, [-1, num_capsules, capsules_dims])
            x = self._squash(x, axis=2)
            tf.logging.info('image after primal capsules {}'.format(x.get_shape()))

        if not self.hps.standard:
            # EXPERIMENT: adding multilayer capsules
            with tf.variable_scope('digital_capsules_0'):
                params_shape = [16, num_capsules, filters[3], capsules_dims, capsules_dims]
                x = self._capsule_layer(x, params_shape=params_shape,
                                        num_routing=self.hps.num_routing)

        with tf.variable_scope('digital_capsules_final'):
            """
                params_shape = [output_num_capsule, input_num_capsule,
                                output_dim_capsule, input_dim_capsule]
            """
            params_shape = [self.hps.num_classes, num_capsules, filters[3], capsules_dims]
            cigits = self._capsule_layer(x, params_shape=params_shape,
                                         num_routing=self.hps.num_routing, name='capsules_final_cigits')
            tf.logging.info('cigits shape {}'.format(cigits.get_shape()))


            # Compute length of each [None,output_num_capsule,output_dim_capsule]
            #y_pred = tf.sqrt(tf.reduce_sum(tf.square(cigits), 2))
            self.y_pred = self._fully_connected(cigits, self.hps.num_labels, name='capsules_final_fc', dropout_prob=0.5)

        with tf.variable_scope('costs'):
            self.L, self.y_pred_flipped = cost_tensor(self.y_pred, self.labels)
            cost = self.L #+ self._decay()
            tf.summary.scalar('Prediction_loss', self.L)
            tf.summary.scalar('Total_loss', cost)
        return cost

class CapsNet2(CapsNet):
    """CapsNet2 model.
       Refactor squash function
       Refactor _capsule_layer function
    """

    def __init__(self, hps, images, labels):
        super().__init__(hps, images, labels)

    def _squash(self, x, axis=-1):
        """https://arxiv.org/pdf/1710.09829.pdf (eq.1)

           squash activation that normalizes vectors by their relative lengths
        """
        square_norm = tf.reduce_sum(tf.square(x), axis=axis, keep_dims=True)
        x = x * tf.sqrt(square_norm) / (1.0 + square_norm)
        return x

    def _capsule_layer(self, x, params_shape, num_routing, name=''):

        assert len(params_shape) == 4, "Given wrong parameter shape."
        output_num_capsule, input_num_capsule, output_dim_capsule, input_dim_capsule = params_shape

        # x = self._batch_norm(name + '/bn', x)

        # W.shape =  [None, input_num_capsule, input_dim_capsule, output_num_capsule, output_dim_capsule]
        W = tf.get_variable(
            name + '/capsule_layer_transformation_matrix', [1, input_num_capsule, input_dim_capsule, output_num_capsule, output_dim_capsule], tf.float32,
            initializer=tf.contrib.layers.variance_scaling_initializer(factor=1.0, mode='FAN_AVG', uniform=True))

        # b.shape = [None, self.intput_num_capsule, 1, self.output_num_capsule,1].
        b = tf.get_variable(name + '/coupling_coefficient_logits', [1, input_num_capsule,1,output_num_capsule,1], initializer=tf.zeros_initializer())
        c = tf.stop_gradient(tf.nn.softmax(b, dim=3))

        # u.shape=[None, input_num_capsule, input_dim_capsule, 1, 1]
        u = tf.expand_dims(tf.expand_dims(x, -1),-1)
        u_ = tf.reduce_sum(u * W, axis=[2], keep_dims=True)
        s = tf.reduce_sum(u_ * c, axis=[1], keep_dims=True)
        v = self._squash(s, axis=-1)
        tf.logging.info('Expanding inputs to be {}'.format(u.get_shape()))
        tf.logging.info('Transforming and sum input capsule dimension (routing inputs){}'.format(u_.get_shape()))
        tf.logging.info('Outputs of each routing iteration {}'.format(v.get_shape()))

        assert num_routing > 0, 'The num_routing should be > 0.'

        for i in range(num_routing-1):
            b += tf.reduce_sum(u_ * v,axis=-1,keep_dims=True)
            c =  tf.nn.softmax(b, dim=3)
            s = tf.reduce_sum(u_ * c, axis=[1], keep_dims=True)
            v = self._squash(s,axis=-1)

        v_digit = tf.squeeze(v, axis=[1,2])
        tf.logging.info('Image after this capsule layer {}'.format(v_digit.get_shape()))
        return v_digit
