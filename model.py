import torchvision.models
import torch
import function, blocks
import torch.nn.functional as F
import torch.nn as nn

class Encoder(torch.nn.Module):
    """ Generic encoder that encodes an image into a fixed size vector using down convolutions. """

    def __init__(self, output_dim, in_channels=3, normalization=True, residual=True, num_down_convolutions=6):
        """ Initializes the encoder.
        
        Parameters:
        -----------
        output_dim : int
            The size of the embedding.
        in_channels : int
            The number of input channels.
        normalization : bool
            If True, instance normalization is applied after a down convolution.
        residual : bool
            If True, down convolutions use residual blocks.
        """
        super().__init__()
        self.normalization = normalization
        self.num_down_convolutions = num_down_convolutions
        dims = [min(512, 64*2**i) for i in range(num_down_convolutions)]

        self.convs = nn.ModuleList(
            blocks.DownsamplingConvolution(c_in, c_out, residual=residual) for c_in, c_out in zip([in_channels] + dims[:-1], dims)
        )

        if self.normalization:
            self.norms = nn.ModuleList(
                nn.InstanceNorm2d(c, affine=True) for c in dims
            )
        
        self.pool = nn.AdaptiveMaxPool2d((1, 1))
        self.fc = nn.Linear(dims[-1], output_dim, bias=True)

    def forward(self, x):
        """ Forward pass.
        
        Parameters:
        -----------
        x : torch.Tensor, shape [B, channels_in, H, W]
            Inputs to the encoder.
        
        Returns:
        --------
        z : torch.Tensor, shape [B, output_dim]
        """
        out = x
        for idx in range(self.num_down_convolutions):
            out = self.convs[idx](out)
            if self.normalization:
                out = self.norms[idx](out)
        out = F.relu(self.pool(out), inplace=False)
        out = self.fc(out.view(out.size(0), -1))
        return out

class Decoder(torch.nn.Module):
    """ General purpose decoder that uses AdaIn layers to modify the embedding multiple times and then uses
    TransposeConvs to create the output image. """

    def __init__(self, content_dim, style_dim, resolution, out_channels=3, residual=True, normalization='adain', num_up_convolutions=6):
        """ Initializes the generic decoder.
        
        Parameters:
        -----------
        content_dim : int
            The content embedding dimensionality.
        style_dim : int or None
            The style embedding dimensionality.
        resolution : int or tuple of ints (H, W)
            The output resultion, a power of 2.
        out_channels : int
            The number of output channels.
        residual : bool
            If the upsampling convolutions are implemented by residual blocks.
        normalization : 'in', 'adain' or None
            Which kind of normalization to use. 
        num_up_convolutions : int
            The number of upsampling convolutions.
        """
        super().__init__()
        self.content_dim = content_dim
        self.style_dim = style_dim
        self.residual = residual
        self.normalization = normalization
        self.num_up_convolutions = num_up_convolutions

        dims = list(reversed([min(512, 64*2**i) for i in range(num_up_convolutions)]))

        # The content input has to be reshaped to a spatial dimension, calculate the spatial dimensions
        if isinstance(resolution, int):
            resolution = (resolution, resolution)
        self.in_height = resolution[0] // 2**self.num_up_convolutions
        self.in_width = resolution[1] // 2**self.num_up_convolutions
        self.in_channels = dims[0] 

        self.convs = nn.ModuleList(
            blocks.UpsamplingConvolution(c_in, c_out, style_dim=style_dim, instance_normalization=self.normalization, residual=self.residual)
            for c_in, c_out in zip(dims, dims[1:] + [out_channels])
        )


    def forward(self, content_encoding, style_encoding=None):
        """ Forward pass.
        
        Parameters:
        -----------
        content_encoding : torch.Tensor, shape [batch_size, content_dim]
            Encoding of the content image.
        style_encoding : torch.Tensor, shape [batch_size, style_dim]
            Style encoding of the style image to apply to the content image.
        
        Returns:
        --------
        stylized : torch.Tensor, shape [batch_size, , H, W]
            The stylized output image, where H and W are specified by the resolution given to the decoder initialization.
        """
        c = content_encoding.view(-1, self.in_channels, self.in_height, self.in_width)
        for idx in range(self.num_up_convolutions):
            c = self.convs[idx](c, style_encoding=style_encoding)
        return c

class VGGEncoder(torch.nn.Module):
    """ Encoder network that contains of the first few layers of the vgg11 [1] network. 
    
    References:
    -----------
    [1] : https://arxiv.org/pdf/1409.1556.pdf
    """
    
    
    def __init__(self, input_dim, n_layers=1000, architecture=torchvision.models.vgg11, pretrained=True, flattened_output_dim=None, mean_std_projection=False):
        """ Initializes an encoder model based on some (pretrained) architecture. 
        
        Parameters:
        -----------
        input_dim : int, int, int
            Number of channels, image height and image width of the inputs.
        n_layers : int
            How many layers of the architecture are used for the encoder.
        architecture : torch.nn.module
            A torchvision architecture that allows the first n layers to be extracted.
        pretrained : bool
            If True, pretrained weights of the architecture are used.
        flattened_output_dim : int or None
            If given, the output is flattened and transformed to this dimension. Used for encoding styles into a fixed-size
            latent space.
        """
        super().__init__()
        self.layers = architecture(pretrained=pretrained, progress=True).features[:n_layers]
        self.flattened_output_dim = flattened_output_dim
        self.mean_std_projection = mean_std_projection
        if self.flattened_output_dim:
            C_out, W_out, H_out = vgg_get_output_dim(self.layers, input_dim)
            self.projection = torch.nn.Sequential(
                torch.nn.modules.Linear(H_out * W_out * C_out, 4096),
                torch.nn.Dropout(0.5, inplace=False),
                torch.nn.ReLU(),
                torch.nn.modules.Linear(4096, self.flattened_output_dim),
                torch.nn.Dropout(0.5, inplace=False),
                torch.nn.ReLU(),
            )
        if self.mean_std_projection:
            C_out, W_out, H_out = self.output_dim(input_dim)
            self.projection_mean = torch.nn.modules.Linear(H_out * W_out * C_out, 1)
            self.projection_std = torch.nn.modules.Linear(H_out * W_out * C_out, 1)

        


    def forward(self, input):
        """ Forward pass through the encoder network. 
        
        Parameters:
        -----------
        input : torch.Tensor, shape [batch_size, 3, width, height]
            A batch of images to encode.

        Returns:
        --------
        output : torch.Tensor, shape [batch_size, out_features, width / scale, height / scale]
            Feature maps for the images.
        """
        output = self.layers(input)
        if self.flattened_output_dim:
            B = output.size()[0]
            output = self.projection(output.view(B, -1))
        if self.mean_std_projection:
            B = output.size()[0]
            mean = self.projection_mean(output.view(B, -1))
            var = self.projection_std(output.view(B, -1))
            output = (mean, var)
        return output

   
def vgg_get_output_dim(layers, input_dim):
    """ Dynamically calculates the output dimensionality of a sequential architecture. 
    
    Parameters:
    -----------
    layers : iterable of torch.nn.Module
        A set of layers that are applied to the image. MaxPool2ds are considered.
    input_dim : int, int, int
        Channel, Height and Width of the input image.
    
    Returns:
    --------
    output_dim : int, int, int
        Channel, Height and Width of the output image.
    """
    C, H, W = input_dim
    num_poolings = sum(map(lambda layer: isinstance(layer, torch.nn.modules.MaxPool2d), layers))
    C_out = list(filter(lambda layer: isinstance(layer, torch.nn.modules.Conv2d), layers))[-1].out_channels
    return C_out, H // 2**num_poolings, W // 2**num_poolings


class VGGDecoder(torch.nn.Module):
    """ Decoder network that mirrors the structure of an encoder architecture. """

    def __init__(self, image_dim, style_dim, content_dim, n_layers=21, architecture=torchvision.models.vgg11):
        """ Initializes a decoder model that tries to mirror the encoder architecture.
        
        Parameters:
        -----------
        style_dim : int
            Style embedding dimensionality.
        n_layers : int
            How many layers of the architecture are used for the decoder.
        architecture : torchvision.model
            A torchvision architecture that allows the first n layers to be extracted for the decoder.
        """
        super().__init__()
        self.image_dim = image_dim
        self.content_dim = content_dim
        self.style_dim = style_dim
        
        C_in, H_in, W_in = vgg_get_output_dim(architecture(pretrained=False, progress=True).features[:n_layers], self.image_dim)
        self.input_dim = C_in, H_in, W_in # Input shape for the decoder convolutions
        if isinstance(self.content_dim, int): #TODO: we should always input a flat vector...
            self.fc = torch.nn.Sequential(
                torch.nn.Linear(self.content_dim, 4096),
                torch.nn.Dropout(0.5, inplace=False),
                torch.nn.ReLU(inplace=False),
                torch.nn.Linear(4096, C_in * H_in * W_in),
                torch.nn.Dropout(0.5, inplace=False),
                torch.nn.ReLU(inplace=False),
            )

        self.layers = torch.nn.modules.ModuleList()
        #slice_idx = 0
        conv_layer_added = False # The first layer should be a convolution, ignore the first layers that are non convolutional
        for idx, layer in enumerate(reversed(architecture(pretrained=False, progress=True).features[:n_layers])):
            if isinstance(layer, torch.nn.modules.Conv2d):
                self.layers.append(torch.nn.modules.ReflectionPad2d(1))
                conv = torch.nn.modules.Conv2d(layer.out_channels, layer.in_channels, kernel_size=layer.kernel_size, 
                    stride=layer.stride, dilation=layer.dilation)
                self.layers.append(conv)
                #self.layers.append(AdaInLayer(style_dim, conv.out_channels, slice_idx))
                #slice_idx += conv.out_channels * 2
                #print(slice_idx)
                #self.layers.append(torch.nn.InstanceNorm2d(conv.out_channels, affine=True))
                conv_layer_added = True
            elif isinstance(layer, torch.nn.modules.MaxPool2d):
                self.layers.append(torch.nn.modules.UpsamplingNearest2d(scale_factor=layer.stride))
            elif isinstance(layer, torch.nn.modules.ReLU):
                if not conv_layer_added: continue
                self.layers.append(torch.nn.modules.ReLU(inplace=False))
            else:
                raise NotImplementedError('Decoder implementation can not mirror {type(layer)} layer.')

    def forward(self, content, style=None):
        """ Forward pass through the decoder network. 
        
        Parameters:
        -----------
        content : torch.Tensor, shape [batch_size, out_features, width, height]
            A batch of images to decode.
        style : torch.Tensor, shape [batch_size, style_dim] or None
            Optional: A style encoding that is used during AdaIn layers.


        Returns:
        --------
        output : torch.Tensor, shape [batch_size, 3, width * scale, height * scale]
            Decoded images.
        """
        if isinstance(self.content_dim, int):
            C_in, H_in, W_in = self.input_dim
            content = self.fc(content).view(-1, C_in, H_in, W_in)
        for layer in self.layers:
            if isinstance(layer, blocks.AdaInBlock):
                if style is not None:
                    content = layer(content, style)
            else:
                content = layer(content)

        return content
        #return torch.sigmoid(content)
    
