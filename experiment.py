"""Functionality for running an experiment with a single set of hyperparameters."""

from ast import literal_eval
from collections import namedtuple
import inspect
from random import Random
from time import time
import sys
from types import FunctionType

from commandr import command
import lasagne
import numpy as np
from sklearn import utils as skutils
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler

from sklearn.svm import LinearSVC
import theano

from theano_latest.misc import pkl_utils
from architectures import ARCHITECTURE_NAME_TO_CLASS
from util import parse_param_str

_MIN_LEARNING_RATE = 1e-10
_LEARNING_RATE_GRACE_PERIOD = 3


@command
def run_experiment(dataset_path, model_architecture, model_params=None, num_epochs=5000, batch_size=100,
                   chunk_size=0, verbose=False, reshape_to=None, update_func_name='nesterov_momentum',
                   learning_rate=0.01, update_func_kwargs=None, adapt_learning_rate=False, subtract_mean=True,
                   labels_to_keep=None, snapshot_every=0, snapshot_prefix='model', start_from_snapshot=None,
                   snapshot_final_model=True, num_crops=0, crop_shape=None, mirror_crops=True, test_only=False):
    """Run a deep learning experiment, reporting results to standard output.

    Command line or in-process arguments:
     * dataset_path (str) - path of dataset pickle zip (see data.create_datasets)
     * model_architecture (str) - the name of the architecture to use (subclass of architectures.AbstractModelBuilder)
     * model_params (str) - colon-separated list of equals-separated key-value pairs to pass to the model builder.
                            All keys are assumed to be strings, while values are evaluated as Python literals
     * num_epochs (int) - number of training epochs to run
     * batch_size (int) - number of examples to feed to the network in each batch
     * chunk_size (int) - number of examples to copy to the GPU in each chunk. If it's zero, the chunk size is set to
                          the number of training examples, which results in faster training. However, it's impossible
                          when the size of the example set is larger than the GPU's memory
     * verbose (bool) - if True, extra debugging information will be printed
     * reshape_to (str) - if given, the data will be reshaped to match this string, which should evaluate to a Python
                          tuple of ints (e.g., may be required to make the dataset fit into a convnet input layer)
     * update_func_name (str) - update function to use to train the network. See functions with signature
                                lasagne.updates.<update_func_name>(loss_or_grads, params, learning_rate, **kwargs)
     * learning_rate (float) - learning rate to use with the update function
     * update_func_kwargs (str) - keyword arguments to pass to the update function in addition to learning_rate. This
                                  string has the same format as model_params
     * adapt_learning_rate (bool) - if True, the learning rate will be reduced by a factor of 10 when the validation
                                    loss hasn't decreased within _LEARNING_RATE_GRACE_PERIOD, down to a minimum of
                                    _MIN_LEARNING_RATE
     * subtract_mean (bool) - if True, the mean RGB value in the training set will be subtracted from all subsets
                              of the dataset
     * labels_to_keep (str) - comma-separated list of labels to keep -- all other labels will be dropped
     * snapshot_every (int) - if nonzero, a model snapshot will be save every snapshot_every number of epochs
     * snapshot_prefix (str) - prefix for saved snapshot files
     * start_from_snapshot (str) - path of model snapshot to start training from. Note: currently, the snapshot doesn't
                                   contain all the original hyperparameters, so running this command with
                                   start_from_snapshot still requires passing all the original command arguments
     * snapshot_final_model (bool) - if True, the final model snapshot will be saved
     * num_crops (int) - if non-zero, this number of random crops of the images will be used
     * crop_shape (str) - if given, specifies the shape of the crops to be created (converted to tuple like reshape_to)
     * mirror_crops (bool) - if True, every random crop will be mirrored horizontally, making the effective number of
                             crops 2 * num_crops
     * test_only (bool) - if True, no training will be performed, and results on the testing subset will be reported
    """
    # pylint: disable=too-many-locals,too-many-arguments
    assert theano.config.floatX == 'float32', 'Theano floatX must be float32 to ensure consistency with pickled dataset'
    if model_architecture not in ARCHITECTURE_NAME_TO_CLASS:
        raise ValueError('Unknown architecture %s (valid values: %s)' % (model_architecture,
                                                                         sorted(ARCHITECTURE_NAME_TO_CLASS)))
    # Set a static random seed for reproducibility
    np.random.seed(572893204)
    dataset, label_to_index = _load_data(dataset_path, reshape_to, subtract_mean, labels_to_keep=labels_to_keep)
    learning_rate_var = theano.shared(lasagne.utils.floatX(learning_rate))
    model_builder = ARCHITECTURE_NAME_TO_CLASS[model_architecture](
        dataset, output_dim=len(label_to_index), batch_size=batch_size, chunk_size=chunk_size, verbose=verbose,
        update_func_name=update_func_name, learning_rate=learning_rate_var,
        update_func_kwargs=parse_param_str(update_func_kwargs), num_crops=num_crops,
        crop_shape=literal_eval(crop_shape) if crop_shape else None, mirror_crops=mirror_crops
    )
    start_epoch, output_layer = _load_model_snapshot(start_from_snapshot) if start_from_snapshot else (0, None)
    output_layer, training_iter, validation_eval = model_builder.build(
        output_layer=output_layer, **parse_param_str(model_params)
    )

    if test_only:
        testing_loss, testing_accuracy = model_builder.create_eval_function('testing', output_layer)()
        print('Testing loss & accuracy:\t %.6f\t%.2f%%' % (testing_loss, testing_accuracy * 100))
        return

    _print_network_info(output_layer)
    try:
        _run_training_loop(output_layer, training_iter, validation_eval, num_epochs, snapshot_every, snapshot_prefix,
                           snapshot_final_model, start_epoch, learning_rate_var, adapt_learning_rate)
    except OverflowError, e:
        print('Divergence detected (OverflowError: %s). Stopping now.' % e)
    except KeyboardInterrupt:
        pass


@command
def run_baseline(dataset_path, baseline_name, rf_n_estimators=100, random_state=0, rf_num_iter=10, labels_to_keep=None,
                 test_subset='validation'):
    """Run a baseline classifier (random_forest or linear) on the dataset, printing accuracy on test_subset."""
    dataset, _ = _load_data(dataset_path, flatten=True, labels_to_keep=labels_to_keep)
    if test_subset == 'validation':
        training_instances, training_labels = dataset['training']
    else:
        training_instances, training_labels = (np.concatenate((dataset['training'][i], dataset['validation'][i]))
                                               for i in (0, 1))

    if baseline_name == 'random_forest':
        rnd = Random(random_state)
        scores = []
        for _ in xrange(rf_num_iter):
            estimator = RandomForestClassifier(n_jobs=-1, random_state=hash(rnd.random()), n_estimators=rf_n_estimators)
            estimator.fit(training_instances, training_labels)
            scores.append(estimator.score(*dataset[test_subset]))
        print('Accuracy: {:.4f} (std: {:.4f})'.format(np.mean(scores), np.std(scores)))
    elif baseline_name == 'linear':
        estimator = Pipeline([('scaler', MinMaxScaler()), ('svc', LinearSVC(random_state=random_state))])
        estimator.fit(training_instances, training_labels)
        print('Accuracy: {:.4f}'.format(estimator.score(*dataset[test_subset])))
    else:
        raise ValueError('Unknown baseline_name %s (supported values: random_forest, linear)' % baseline_name)


def _save_model_snapshot(output_layer, snapshot_prefix, next_epoch):
    snapshot_path = '%s.snapshot-%s.pkl.zip' % (snapshot_prefix, next_epoch)
    print('Saving snapshot to %s' % snapshot_path)
    with open(snapshot_path, 'wb') as out:
        pkl_utils.dump((next_epoch, output_layer), out)


def _load_model_snapshot(snapshot_path):
    print('Loading pickled model from %s' % snapshot_path)
    with open(snapshot_path, 'rb') as snapshot_file:
        return pkl_utils.load(snapshot_file)


def _transform_dataset(dataset, func):
    for subset_name, (data, labels) in dataset.iteritems():
        dataset[subset_name] = func(data, labels)


def _load_data(dataset_path, reshape_to=None, subtract_mean=False, flatten=False, labels_to_keep=()):
    with open(dataset_path, 'rb') as dataset_file:
        dataset, label_to_index = pkl_utils.load(dataset_file)
    if labels_to_keep:
        labels_to_keep = set(labels_to_keep.split(','))
        unknown_labels = labels_to_keep.difference(label_to_index)
        if unknown_labels:
            raise ValueError('Unknown labels passed %s' % unknown_labels)
        old_label_index_to_new = dict(zip((label_to_index[l] for l in labels_to_keep), xrange(len(labels_to_keep))))
        old_label_indexes_to_keep = [label_to_index[l] for l in labels_to_keep]
        map_labels = np.vectorize(lambda li: old_label_index_to_new[li], otypes=['int32'])

        def drop_labels(data, labels):
            ind = np.in1d(labels, old_label_indexes_to_keep)
            return data[ind], map_labels(labels[ind])
        _transform_dataset(dataset, drop_labels)
        label_to_index = {l: old_label_index_to_new[label_to_index[l]] for l in labels_to_keep}
    if reshape_to:
        reshape_to = literal_eval(reshape_to)
        _transform_dataset(dataset, lambda data, labels: (data.reshape((data.shape[0], ) + reshape_to), labels))
    if subtract_mean:
        training_mean = np.mean(dataset['training'][0], axis=0, dtype='float32')
        _transform_dataset(dataset, lambda data, labels: (data - training_mean, labels))
    if flatten:
        _transform_dataset(dataset,
                           lambda data, labels: ((data.reshape((data.shape[0], np.prod(data.shape[1:]))), labels)
                                                 if len(data.shape) > 2 else (data, labels)))
    _transform_dataset(dataset, skutils.shuffle)
    return dataset, label_to_index


def _get_default_init_kwargs(obj):
    args, _, _, defaults = inspect.getargspec(obj.__init__)
    return dict(zip(reversed(args), reversed(defaults)))


def _print_network_info(output_layer):
    print('Network architecture:')
    sum_params = 0
    sum_memory = 0.0
    for layer in lasagne.layers.get_all_layers(output_layer):
        init_kwargs = _get_default_init_kwargs(layer)
        filtered_params = {}
        for key, value in layer.__dict__.iteritems():
            if key.startswith('_') or key in ('name', 'input_var', 'input_layer', 'W', 'b', 'params') or \
               (key in init_kwargs and init_kwargs[key] == value):
                continue
            if isinstance(value, FunctionType):
                value = value.__name__
            filtered_params[key] = value
        layer_args = ', '.join('%s=%s' % (k, v) for k, v in sorted(filtered_params.iteritems()))
        num_layer_params = sum(np.prod(p.get_value().shape) for p in layer.get_params())
        layer_memory = (np.prod(layer.output_shape) + num_layer_params) * 4 / 2. ** 20
        print('\t{:}({:}): {:,} parameters {:.2f}MB'.format(layer.__class__.__name__, layer_args, num_layer_params,
                                                            layer_memory))
        sum_params += num_layer_params
        sum_memory += layer_memory
    print('Sums: {:,} parameters {:.2f}MB'.format(sum_params, sum_memory))


_MaxState = namedtuple('MaxState', ('accuracy', 'epoch', 'params'))


def _run_training_loop(output_layer, training_iter, validation_eval, num_epochs, snapshot_every, snapshot_prefix,
                       snapshot_final_model, start_epoch, learning_rate_var, adapt_learning_rate):
    now = time()
    validation_loss, validation_accuracy = validation_eval()
    print('Initial validation loss & accuracy:\t %.6f\t%.2f%%' % (validation_loss, validation_accuracy * 100))
    sys.stdout.flush()

    max_state = None
    for epoch in xrange(start_epoch, num_epochs):
        training_loss = training_iter()
        validation_loss, validation_accuracy = validation_eval()
        next_epoch = epoch + 1
        print('Epoch %s of %s took %.3fs' % (next_epoch, num_epochs, time() - now))
        now = time()
        print('\ttraining loss:\t\t\t %.6f' % training_loss)
        print('\tvalidation loss & accuracy:\t %.6f\t%.2f%%' % (validation_loss, validation_accuracy * 100))
        sys.stdout.flush()

        if snapshot_every and next_epoch % snapshot_every == 0:
            _save_model_snapshot(output_layer, snapshot_prefix, next_epoch)

        if adapt_learning_rate:
            if max_state is None or validation_accuracy > max_state.accuracy:
                max_state = _MaxState(validation_accuracy, epoch, lasagne.layers.get_all_param_values(output_layer))

            if validation_accuracy <= max_state.accuracy and epoch - max_state.epoch > _LEARNING_RATE_GRACE_PERIOD:
                new_learning_rate = learning_rate_var.get_value() / lasagne.utils.floatX(10)
                if new_learning_rate < _MIN_LEARNING_RATE:
                    print('Reached minimum learning rate. Stopping now.')
                    break
                learning_rate_var.set_value(new_learning_rate)
                lasagne.layers.set_all_param_values(output_layer, max_state.params)
                max_state = _MaxState(max_state.accuracy, epoch, max_state.params)
                print('Validation accuracy not increased from max, reducing learning rate to %.0e' % new_learning_rate)

    if snapshot_final_model:
        print('Training finished -- saving final model')
        _save_model_snapshot(output_layer, snapshot_prefix, next_epoch)
