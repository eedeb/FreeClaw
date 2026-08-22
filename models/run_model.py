import json
from time import sleep
import nltk
from nltk.tokenize import word_tokenize
import random
import math

nltk.download("punkt_tab")

remove_punctuation = str.maketrans("", "", "!\"#&'(),.:;?@[\\]_`{|}~")

# Math
def activation(x):
    return 1 / (1 + math.exp(-x))

def activation_derivative(s):
    return 1

def softmax(logits):
    m = max(logits)
    exps = [math.exp(z - m) for z in logits]
    total = sum(exps)
    return [e / total for e in exps]

# Weights load on first classify() and stay cached, so importing this module
# costs nothing and the caller decides where model.json lives.
_loaded_path=None
dictionary=None
tags=None
hidden_layer=None
hidden_bias=None
output_layer=None
output_bias=None

def load_model(path):
    global _loaded_path, dictionary, tags, hidden_layer, hidden_bias, output_layer, output_bias
    if path==_loaded_path:
        return
    with open(path) as f:
        model=json.load(f)
    dictionary=model["dictionary"]
    tags=model["tags"]
    hidden_layer=model["hidden_layer"]
    hidden_bias=model["hidden_bias"]
    output_layer=model["output_layer"]
    output_bias=model["output_bias"]
    _loaded_path=path

# Bag

def build_bow(phrase,dictionary):
    bag=[]
    tokenized=word_tokenize(phrase.lower().translate(remove_punctuation))
    for token in dictionary:
        if token in tokenized:
            bag.append(1)
        else:
            bag.append(0)
    return bag
def optimize_bow(bag):
    optimized_bag=[]
    for item_index, item in enumerate(bag):
        if item == 1:
            optimized_bag.append(item_index)
    return optimized_bag

# Forward Pass
def forward_pass(optimized_bag, bag):
    # Hidden Layer
    hidden_output=[]
    for neuron_index, neuron in enumerate(hidden_layer):
        #neuron=[0.123, 0.456, ...]
        output=0


        for item_index in optimized_bag:
            # for float in previous layer
            output+=1*neuron[item_index]


        output+=hidden_bias[neuron_index]
        output=activation(output)

        hidden_output.append(output)

    # Output Layer
    output_output=[]
    logits=[]
    for neuron_index, neuron in enumerate(output_layer):
        #neuron=[0.123, 0.456, ...]
        output=0

        
        for item_index, item in enumerate(hidden_output):
            # for float in previous layer
            output+=item*neuron[item_index]


        output+=output_bias[neuron_index]
        #output=activation(output)

        logits.append(output)
    output_output=softmax(logits)
    return output_output, hidden_output



def classify(phrase, path):
    load_model(path)
    bag=build_bow(phrase,dictionary)
    optimize_bag=optimize_bow(bag)
    output, hidden_output=forward_pass(optimize_bag,bag)
    # output=[0.123, 0.456, 0.789, ...]
    # tags=["greeting", "goodbye", "thanks", ...]
    sorted_tags=[]
    certainty=sorted(output, reverse=True)
    for index, value in enumerate(certainty):
        output_index=output.index(value)
        sorted_tags.append(tags[output_index])
    return sorted_tags, certainty

#print(classify("write a story about a duck"))