from tensorflow.keras.applications.imagenet_utils import preprocess_input, decode_predictions
from tensorflow.keras.models import load_model
from tensorflow.keras.preprocessing import image
from tensorflow.keras.applications.resnet50 import ResNet50
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from pathlib import Path
import uvicorn, aiohttp, asyncio
import sys, numpy as np
import time

# imports from jupyter
import tensorflow as tf
import pandas as pd
import numpy as np
from keras_preprocessing.image import ImageDataGenerator
from keras.applications.vgg16 import preprocess_input
from tensorflow.keras.applications.vgg16 import VGG16
from tensorflow.keras.applications import ResNet50
from tensorflow.keras import Sequential
from tensorflow.keras.layers import GlobalAveragePooling2D, Dense, Dropout, Flatten, Input, Conv2D, multiply, LocallyConnected2D, Lambda, BatchNormalization
from tensorflow.keras.models import Model
from keras.metrics import mean_absolute_error
import math

path = Path(__file__).parent

# Github model link
female_model_file_url = 'https://github.com/tvaingart/bone-age-assessment-webapp/blob/main/models/female_model_weights_resnet.h5?raw=true'
male_model_file_url = 'https://github.com/tvaingart/bone-age-assessment-webapp/blob/main/models/male_model_weights_vgg.h5?raw=true'
male_model_file_name = 'male_model'
female_model_file_name = 'female_model'

MALE_MODEL_PATH = path/'models'/f'{male_model_file_name}.h5'
FEMALE_MODEL_PATH = path/'models'/f'{female_model_file_name}.h5'


app = Starlette()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_headers=['X-Requested-With', 'Content-Type'])
app.mount('/static', StaticFiles(directory='app/static'))



IMG_FOLDER = '/tmp/'
IMG_FILE_SRC = '/tmp/saved_image.png'
REL_IMG_FILE_SRC = 'saved_image.png'

x_col = 'path'
y_col = 'boneage'
width = height = 384
target_size = (width, height)
boneage_mean = 0
boneage_div = 1.0



def get_attention(input_shape=(384, 384, 3)):
  from tensorflow.keras.applications.vgg16 import VGG16
  from tensorflow.keras.layers import GlobalAveragePooling2D, Dense, Dropout, Flatten, Input, Conv2D, multiply, LocallyConnected2D, Lambda, BatchNormalization
  from tensorflow.keras.models import Model
  in_lay = Input(input_shape)
  base_pretrained_model = VGG16(input_shape =  input_shape, include_top = False, weights = 'imagenet')
  base_pretrained_model.trainable = False
  pt_features = base_pretrained_model(in_lay)
  pt_depth = base_pretrained_model.get_output_shape_at(0)[-1]
  bn_features = BatchNormalization()(pt_features)

  # here we do an attention mechanism to turn pixels in the GAP on an off

  attn_layer = Conv2D(64, kernel_size = (1,1), padding = 'same', activation = 'relu')(bn_features)
  attn_layer = Conv2D(16, kernel_size = (1,1), padding = 'same', activation = 'relu')(attn_layer)
  attn_layer = LocallyConnected2D(1, 
                                  kernel_size = (1,1), 
                                  padding = 'valid', 
                                  activation = 'sigmoid')(attn_layer)
  # fan it out to all of the channels
  up_c2_w = np.ones((1, 1, 1, pt_depth))
  up_c2 = Conv2D(pt_depth, kernel_size = (1,1), padding = 'same', 
                activation = 'linear', use_bias = False, weights = [up_c2_w])
  up_c2.trainable = False
  attn_layer = up_c2(attn_layer)

  mask_features = multiply([attn_layer, bn_features])
  gap_features = GlobalAveragePooling2D()(mask_features)
  gap_mask = GlobalAveragePooling2D()(attn_layer)
  # to account for missing values from the attention model
  gap = Lambda(lambda x: x[0]/x[1], name = 'RescaleGAP')([gap_features, gap_mask])
  gap_dr = Dropout(0.5)(gap)
  dr_steps = Dropout(0.25)(Dense(1024, activation = 'elu')(gap_dr))
  out_layer = Dense(1, activation = 'linear')(dr_steps) # linear is what 16bit did
  bone_age_model = Model(inputs = [in_lay], outputs = [out_layer])
  bone_age_model.summary()
  return bone_age_model


# get resnet model
def get_resnet_model(input_shape=(384, 384, 3)):
  in_lay = Input(input_shape)

  base_pretrained_model = ResNet50(input_shape =  input_shape, include_top = False, weights = 'imagenet')

  #base_pretrained_model.summary()
  base_pretrained_model.trainable = False
  pt_features = base_pretrained_model(in_lay)
  pt_depth = base_pretrained_model.get_output_shape_at(0)[-1]
  bn_features = BatchNormalization()(pt_features)

  # here we do an attention mechanism to turn pixels in the GAP on an off

  attn_layer = Conv2D(64, kernel_size = (1,1), padding = 'same', activation = 'relu')(bn_features)
  attn_layer = Conv2D(16, kernel_size = (1,1), padding = 'same', activation = 'relu')(attn_layer)
  attn_layer = LocallyConnected2D(1, 
                                  kernel_size = (1,1), 
                                  padding = 'valid', 
                                  activation = 'sigmoid')(attn_layer)
  # fan it out to all of the channels
  up_c2_w = np.ones((1, 1, 1, pt_depth))
  up_c2 = Conv2D(pt_depth, kernel_size = (1,1), padding = 'same', 
                activation = 'linear', use_bias = False, weights = [up_c2_w])
  up_c2.trainable = False
  attn_layer = up_c2(attn_layer)

  mask_features = multiply([attn_layer, bn_features])
  gap_features = GlobalAveragePooling2D()(mask_features)
  gap_mask = GlobalAveragePooling2D()(attn_layer)
  # to account for missing values from the attention model
  gap = Lambda(lambda x: x[0]/x[1], name = 'RescaleGAP')([gap_features, gap_mask])
  gap_dr = Dropout(0.5)(gap)
  dr_steps = Dropout(0.25)(Dense(1024, activation = 'elu')(gap_dr))
  out_layer = Dense(1, activation = 'linear')(dr_steps) # linear is what 16bit did
  bone_age_model = Model(inputs = [in_lay], outputs = [out_layer])
  #bone_age_model.summary()
  return bone_age_model


def predict(img_path, is_male=True, model=None):
  print('trying to predict internal')
  img = load_image(img_path, False)
  # preprocess: 
  test_datagen = ImageDataGenerator(preprocessing_function = preprocess_input)
  fake_test_df = pd.DataFrame({
                'id': [1],
                'boneage    male': [is_male], 
                'boneage_zscore': ['000'],
                'boneage_category': ['no_cat'],
                'rel_path': [REL_IMG_FILE_SRC],
                'path': [IMG_FILE_SRC]
                })
  print('before generator')
  test_generator = test_datagen.flow_from_dataframe(
      fake_test_df, directory=IMG_FOLDER, x_col=x_col, 
      y_col='boneage_zscore', target_size=target_size, color_mode='rgb',
      batch_size=1, shuffle=False,
      class_mode = 'sparse', validate_filenames=False)
  test_generator.reset()
  start = time.time()
  test_steps = math.ceil((len(test_generator.classes) / 64))
  months_prediction = model.predict_generator(test_generator)
  print('months prediction ' + str(months_prediction))
  score = model.evaluate(test_generator, steps=test_steps)
  total_time = time.time() - start
  print('model output: ', score)
  return [score[1], total_time]
  
def load_image(img_path, show=False):
    img = image.load_img(img_path)
    img_tensor = image.img_to_array(img)                    # (height, width, channels)
    img_tensor = np.expand_dims(img_tensor, axis=0)         # (1, height, width, channels), add a dimension because the model expects this shape: (batch_size, height, width, channels)
    img_tensor /= 255.                                      # imshow expects values in the range [0, 1]

    if show:
        plt.imshow(img_tensor[0])                           
        plt.axis('off')
        plt.show()
    return img_tensor

async def download_file(url, dest):
    if dest.exists(): return
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.read()
            with open(dest, 'wb') as f: f.write(data)

def mae_months(in_gt, in_pred):
  return mean_absolute_error(boneage_div*in_gt, boneage_div*in_pred)

async def setup_model_male():
    #UNCOMMENT HERE FOR CUSTOM TRAINED MODEL
    # LOAD MALE
    await download_file(male_model_file_url, MALE_MODEL_PATH)
    male_model = get_attention()
    male_model.load_weights(MALE_MODEL_PATH)
    male_model.compile(optimizer = 'adam', loss = 'mse', metrics = [mae_months])
    return male_model

async def setup_model_female():
    # LOAD FEMALE
    await download_file(female_model_file_url, FEMALE_MODEL_PATH)
    female_model = get_resnet_model()
    female_model.load_weights(FEMALE_MODEL_PATH)
    female_model.compile(optimizer = 'adam', loss = 'mse', metrics = [mae_months])

    return female_model

# Asynchronous Steps
loop = asyncio.get_event_loop()
tasks = [asyncio.ensure_future(setup_model_male())]
model_male = loop.run_until_complete(asyncio.gather(*tasks))[0]
#loop.close()

# Asynchronous Steps
#female_model = setup_model_female()
tasks2 = [asyncio.ensure_future(setup_model_female())]
model_female = loop.run_until_complete(asyncio.gather(*tasks2))[0]
loop.close()


@app.route("/upload", methods=["POST"])
async def upload(request):
    print("upload called")
    data = await request.form()
    img_bytes = await (data["file"].read())
    print(data)
    with open(IMG_FILE_SRC, 'wb') as f: f.write(img_bytes)
    if "sex" in data and data["sex"] == 'male':
        print('running male model')
        score, time = predict(IMG_FILE_SRC, is_male=True, model=model_male)
        model_arch = 'VGG16'
    else:        
        print('running female model')
        score, time = predict(IMG_FILE_SRC, is_male=False, model=model_female)
        model_arch = 'ResNet50'

    return draw_perdiction(score, time, model_arch)


def draw_perdiction(score, time, model_arch):
    result_html1 = path/'static'/'result1.html'
    result_html2 = path/'static'/'result2.html'
    round_score = "{:.2f}".format(score)
    round_time = "{:.2f}".format(time)
    result_html = str(result_html1.open().read() 
        + " Predicted age: " + str(round_score) + " months <br>"
        + " Prediction time: " + str(round_time) + " miliseconds <br> "
        + " Model architecture: " + str(model_arch)
        + result_html2.open().read())
    return HTMLResponse(result_html)


def model_predict(img_path, model):
    print("going to model_predict!!!")
    result = []; img = image.load_img(img_path, target_size=(384, 384))
    x = preprocess_input(np.expand_dims(image.img_to_array(img), axis=0))
    predictions = decode_predictions(model.predict(x), top=3)[0] # Get Top-3 Accuracy
    for p in predictions: _,label,accuracy = p; result.append((label,accuracy))
    result_html1 = path/'static'/'result1.html'
    result_html2 = path/'static'/'result2.html'
    result_html = str(result_html1.open().read() +str(result) + result_html2.open().read())
    return HTMLResponse(result_html)

@app.route("/")
def form(request):
    index_html = path/'static'/'index.html'
    return HTMLResponse(index_html.open().read())

if __name__ == "__main__":
    if "serve" in sys.argv: uvicorn.run(app, host="0.0.0.0", port=8080)
