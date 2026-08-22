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

def sigmoid_derivative(s):
    return s * (1 - s)



# Build dictionary

intents=json.loads(open('intents.json').read())["intents"]
tags=[]
for tag in intents:
    tags.append(tag["tag"])
patterns=[]
for tag in intents:
    tag_patterns=[]
    for pattern in tag["patterns"]:
        tag_patterns.append(pattern)
    patterns.append(tag_patterns) 

dictionary=[]

for tag in tags:
    tag_index=tags.index(tag)
    for pattern in patterns[tag_index]:
        tokens=word_tokenize(pattern.lower().translate(remove_punctuation))
        for token in tokens:
            if token not in dictionary:
                dictionary.append(token)

# Hyperparams
input_size=len(dictionary)
hidden_size=16
output_size=len(tags)
learning_rate=0.001
epochs=100
print("Input size: ", input_size)
print("Hidden size: ", hidden_size)
print("Output size: ", output_size)

# Build hidden layer
hidden_layer=[]
for i in range(hidden_size):
    hidden_neuron=[]
    for j in range(len(dictionary)):
        rand_float=random.uniform(-0.5, 0.5)
        hidden_neuron.append(rand_float)

    hidden_layer.append(hidden_neuron)

hidden_bias=[]
for i in range(hidden_size):
    rand_float=random.uniform(-0.5, 0.5)
    hidden_bias.append(rand_float)


# Build output later
output_layer=[]
for i in range (output_size):
    output_neuron=[]
    for j in range(hidden_size):
        rand_float=random.uniform(-0.5, 0.5)
        output_neuron.append(rand_float)
    output_layer.append(output_neuron)

output_bias=[]
for i in range(output_size):
    rand_float=random.uniform(-0.5, 0.5)
    output_bias.append(rand_float)



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

def backprop(phrase, actual, hidden_output, bag):
    # Find expected output
    expected=[]
    for tag in patterns:
        if phrase in tag:
            expected.append(1)
        else:
            expected.append(0)
    # Expected out, [0,0,0,1,0,0,0,0, ...]

    # Find output deltas
    output_deltas=[]
    for output_index, output in enumerate(actual):
            # For each number 
            expected_output=expected[output_index]
    
            error=expected_output-output
            output_delta=error*sigmoid_derivative(output)
            output_deltas.append(output_delta)
    
    # Find hidden deltas
    hidden_deltas=[]
    for hidden_index, hidden in enumerate(hidden_output):
            # For each number in hidden_output
    
            hidden_error=0
            for delta_index, delta in enumerate(output_deltas):
                hidden_error+=delta*output_layer[delta_index][hidden_index]

            hidden_delta=hidden_error*hidden*(1-hidden)
            hidden_deltas.append(hidden_delta)

    # Adjust output layer

    for output_index, output in enumerate(actual):
        # For each number 
        output_delta=output_deltas[output_index]

        for value_index, hidden_value in enumerate(hidden_output):
            # For each value from the hidden layer/number of connects in each neuron
            output_layer[output_index][value_index]+=learning_rate*output_delta*hidden_value

        output_bias[output_index]+=learning_rate*output_delta

    # Adjust hidden layer

    for hidden_index, hidden in enumerate(hidden_output):
        # For each number 
        hidden_delta=hidden_deltas[hidden_index
                                   ]
        for value_index, hidden_value in enumerate(bag):
            # For each value from the hidden layer/number of connects in each neuron
            hidden_layer[hidden_index][value_index]+=learning_rate*hidden_delta*hidden_value
        
        hidden_bias[hidden_index]+=learning_rate*hidden_delta

epoch=0
for i in range(epochs):
    for tag in patterns:
        for phrase in tag:
            bag=build_bow(phrase, dictionary)
            actual, hidden=forward_pass(bag)
            backprop(phrase,actual,hidden,bag)
    epoch+=1
    print("Epoch: "+str(epoch))
    test="write a story about a duck"
    test_bag = build_bow(test,dictionary)
    actual,_ = forward_pass(test_bag)
    print(actual)

format = {
    "dictionary": dictionary,
    "tags": tags,
    "hidden_layer": hidden_layer,
    "hidden_bias": hidden_bias,
    "output_layer": output_layer,
    "output_bias": output_bias
}

with open("model.json", "w") as f:
    json.dump(format, f, indent=4)
    
