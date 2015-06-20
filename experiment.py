from ast import literal_eval
import inspect
from random import Random
from time import time
import sys
from types import FunctionType

from commandr import command
import lasagne
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import MinMaxScaler
from sklearn.svm import LinearSVC
import theano
from theano_latest.misc import pkl_utils

from architectures import ARCHITECTURE_NAME_TO_CLASS


@command
def run_experiment(dataset_path, model_architecture, model_params=None, num_epochs=500, batch_size=100,
                   training_chunk_size=0, reshape_to=None, learning_rate=0.01, subtract_mean=True,
                   labels_to_keep=None):
    """Run a deep learning experiment, reporting results to standard output.

    Command line or in-process arguments:
     * dataset_path (str) - path of dataset pickle zip (see data.create_datasets)
     * model_architecture (str) - the name of the architecture to use (subclass of architectures.AbstractModelBuilder)
     * model_params (str) - colon-separated list of equals-separated key-value pairs to pass to the model builder.
                            All keys are assumed to be strings, while values are evaluated as Python literals
     * num_epochs (int) - number of training epochs to run
     * batch_size (int) - number of examples to feed to the network in each batch
     * training_chunk_size (int) - number of training examples to copy to the GPU in each chunk. If set to zero, all
                                   examples will be copied. This is faster, but impossible when the size of the training
                                   set is larger than the GPU's memory
     * reshape_to (str) - if given, the data will be reshaped to match this string, which should evaluate to a Python
                          tuple of ints (e.g., may be required to make the dataset fit into a convnet input layer)
     * learning_rate (float) - learning rate to use for training the network
     * subtract_mean (bool) - if True, the mean RGB value in the training set will be subtracted from all subsets
                              of the dataset
     * labels_to_keep (str) - comma-separated list of labels to keep -- all other labels will be dropped
    """
    assert theano.config.floatX == 'float32', 'Theano floatX must be float32 to ensure consistency with pickled dataset'
    if not model_architecture in ARCHITECTURE_NAME_TO_CLASS:
        raise ValueError('Unknown architecture %s (valid values: %s)' % (model_architecture,
                                                                         sorted(ARCHITECTURE_NAME_TO_CLASS)))

    dataset, label_to_index = _load_data(dataset_path, reshape_to, subtract_mean, labels_to_keep=labels_to_keep)
    model_builder = ARCHITECTURE_NAME_TO_CLASS[model_architecture](
        dataset, output_dim=len(label_to_index), batch_size=batch_size, training_chunk_size=training_chunk_size,
        learning_rate=learning_rate
    )
    output_layer, training_iter, validation_eval = model_builder.build(**_parse_model_params(model_params))
    _print_network_info(output_layer)
    _run_training_loop(training_iter, validation_eval, num_epochs)


@command
def run_baseline(dataset_path, baseline_name, rf_n_estimators=100, random_state=0, rf_num_iter=10, labels_to_keep=None):
    """Run a baseline classifier (random_forest or linear) on the dataset, printing the validation subset accuracy."""
    dataset, _ = _load_data(dataset_path, flatten=True, labels_to_keep=labels_to_keep)
    if baseline_name == 'random_forest':
        rnd = Random(random_state)
        scores = []
        for _ in xrange(rf_num_iter):
            estimator = RandomForestClassifier(n_jobs=-1, random_state=hash(rnd.random()), n_estimators=rf_n_estimators)
            estimator.fit(*dataset['training'])
            scores.append(estimator.score(*dataset['validation']))
        print('Validation accuracy: {:.4f} (std: {:.4f})'.format(np.mean(scores), np.std(scores)))
    elif baseline_name == 'linear':
        estimator = Pipeline([('scaler', MinMaxScaler()), ('svc', LinearSVC(random_state=random_state))])
        estimator.fit(*dataset['training'])
        print('Validation accuracy: {:.4f}'.format(estimator.score(*dataset['validation'])))
    else:
        raise ValueError('Unknown baseline_name %s (supported values: random_forest, linear)' % baseline_name)


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
        label_to_index = {l: i for l, i in label_to_index.iteritems() if l in labels_to_keep}
        label_indexes_to_keep = label_to_index.values()

        def drop_labels(data, labels):
            ind = np.in1d(labels, label_indexes_to_keep)
            return data[ind], labels[ind]
        _transform_dataset(dataset, drop_labels)
    if reshape_to:
        reshape_to = literal_eval(reshape_to)
        _transform_dataset(dataset, lambda data, labels: (data.reshape((data.shape[0], ) + reshape_to), labels))
    if subtract_mean:
        training_mean = np.mean(dataset['training'][0], dtype='float32')
        _transform_dataset(dataset, lambda data, labels: (data - training_mean, labels))
    if flatten:
        _transform_dataset(dataset,
                           lambda data, labels: ((data.reshape((data.shape[0], np.prod(data.shape[1:]))), labels)
                                                 if len(data.shape) > 2 else (data, labels)))
    return dataset, label_to_index


def _parse_model_params(model_params):
    param_kwargs = {}
    if model_params:
        for pair in model_params.split(':'):
            key, value = pair.split('=')
            try:
                param_kwargs[key] = literal_eval(value)
            except (SyntaxError, ValueError):
                param_kwargs[key] = value
        print('Parsed model params: {}'.format(sorted(param_kwargs.iteritems())))
    return param_kwargs


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


def _run_training_loop(training_iter, validation_eval, num_epochs):
    now = time()
    try:
        validation_loss, validation_accuracy = validation_eval()
        print('Initial validation loss & accuracy:\t %.6f\t%.2f%%' % (validation_loss, validation_accuracy * 100))

        for epoch in xrange(num_epochs):
            training_loss = training_iter()
            validation_loss, validation_accuracy = validation_eval()
            print('Epoch %s of %s took %.3fs' % (epoch + 1, num_epochs, time() - now))
            now = time()
            print('\ttraining loss:\t\t\t %.6f' % training_loss)
            print('\tvalidation loss & accuracy:\t %.6f\t%.2f%%' % (validation_loss, validation_accuracy * 100))
            sys.stdout.flush()

            if np.isnan(training_loss) or np.isnan(validation_loss) or np.isnan(validation_accuracy):
                print('Divergence detected. Stopping now.')
                break
    except KeyboardInterrupt:
        pass
