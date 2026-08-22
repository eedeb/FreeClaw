import json
from time import sleep
import nltk
from nltk.tokenize import word_tokenize
import random
import math

nltk.download("punkt_tab")

remove_punctuation = str.maketrans("", "", "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

# Math
def sigmoid(x):
    return 1 / (1 + math.exp(-x))

model=json.loads(open('model.json').read())


dictionary=model["dictionary"]
tags=model["tags"]
hidden_layer=model["hidden_layer"]
hidden_bias=model["hidden_bias"]
output_layer=model["output_layer"]
output_bias=model["output_bias"]

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

# Forward Pass
def forward_pass(bag):
    # Hidden Layer
    hidden_output=[]
    for neuron_index, neuron in enumerate(hidden_layer):
        #neuron=[0.123, 0.456, ...]
        output=0


        for item_index, item in enumerate(bag):
            # for float in previous layer
            output+=item*neuron[item_index]


        output+=hidden_bias[neuron_index]
        output=sigmoid(output)

        hidden_output.append(output)

    # Output Layer
    output_output=[]
    for neuron_index, neuron in enumerate(output_layer):
        #neuron=[0.123, 0.456, ...]
        output=0

        
        for item_index, item in enumerate(hidden_output):
            # for float in previous layer
            output+=item*neuron[item_index]


        output+=output_bias[neuron_index]
        output=sigmoid(output)

        output_output.append(output)
    return output_output, hidden_output

def classify(phrase):
    bag=build_bow(phrase,dictionary)
    output, hidden_output=forward_pass(bag)
    # output=[0.123, 0.456, 0.789, ...]
    # tags=["greeting", "goodbye", "thanks", ...]
    sorted_tags=[]
    certainty=sorted(output, reverse=True)
    for index, value in enumerate(certainty):
        output_index=output.index(value)
        sorted_tags.append(tags[output_index])
    return sorted_tags, certainty

print(classify("write a story about a duck"))