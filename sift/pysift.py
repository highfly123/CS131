"""
References:
1. https://lerner98.medium.com/implementing-sift-in-python-36c619df7945
2. https://medium.com/@russmislam/implementing-sift-in-python-a-complete-guide-part-1-306a99b50aa5
3. https://www.cnblogs.com/silence-cho/p/15143439.html
4. https://blog.csdn.net/zddblog/article/details/7521424
"""

from numpy import all, any, array, arctan2, cos, sin, exp, dot, log, logical_and, roll, sqrt, stack, trace, \
    unravel_index, pi, deg2rad, rad2deg, where, zeros, floor, full, nan, isnan, round, float32
from numpy.linalg import det, lstsq, norm
from cv2 import resize, GaussianBlur, subtract, KeyPoint, INTER_LINEAR, INTER_NEAREST
from functools import cmp_to_key
import logging

####################
# Global variables #
####################

logger = logging.getLogger(__name__)
float_tolerance = 1e-7


#################
# Main function #
#################

def computeKeypointsAndDescriptors(image, sigma=1.6, num_intervals=3, assumed_blur=0.5, image_border_width=5):
    """Compute SIFT keypoints and descriptors for an input image
    """
    image = image.astype('float32')
    base_image = generateBaseImage(image, sigma, assumed_blur)
    num_octaves = computeNumberOfOctaves(base_image.shape)
    gaussian_kernels = generateGaussianKernels(sigma, num_intervals)
    gaussian_images = generateGaussianImages(base_image, num_octaves, gaussian_kernels)
    dog_images = generateDoGImages(gaussian_images)
    keypoints = findScaleSpaceExtrema(gaussian_images, dog_images, num_intervals, sigma, image_border_width)
    keypoints = removeDuplicateKeypoints(keypoints)
    keypoints = convertKeypointsToInputImageSize(keypoints)
    descriptors = generateDescriptors(keypoints, gaussian_images)
    return keypoints, descriptors


#########################
# Image pyramid related #
#########################

def generateBaseImage(image, sigma, assumed_blur):
    """
    Generate base image from input image by upsampling by 2 in both directions and blurring.
    In the paper 3.3: Frequency of sampling in the spatial domain.
    1. prior smoothing σ is applied to each image level before building the scale space representation for an octave;
    2. the top line is the repeatability of keypoint detection, and the results show that the repeatability continues to increase with σ, there is a cost to using a large σ in terms of efﬁciency, so the author chosen to use σ = 1.6;
    3. pre-smooth the image before extrema detection, we are effectively discarding the highest spatial frequencies.  ==> double the size of the input image using linear interpolation prior to building the ﬁrst level of the pyramid.
    """
    logger.debug('Generating base image...')
    image = resize(image, (0, 0), fx=2, fy=2, interpolation=INTER_LINEAR)

    sigma_diff = sqrt(
        max(
            (sigma ** 2) - ((2 * assumed_blur) ** 2),
            0.01)
    )
    return GaussianBlur(image, (0, 0), sigmaX=sigma_diff,
                        sigmaY=sigma_diff)  # the image blur is now sigma instead of assumed_blur


def computeNumberOfOctaves(image_shape):
    """Compute number of octaves in image pyramid as function of base image shape (OpenCV default)
    1. halve the base image numOctaves — 1 times to end up with numOctaves layers；
    2. -1 ensure that  the image in the highest octave (the smallest image) will have a side length of at least 3. This is important because we’ll search for minima and maxima in each DoG image later, which means we need to consider 3-by-3 pixel neighborhoods.
    """
    return int(round(log(min(image_shape)) / log(2) - 1))


def generateGaussianKernels(sigma, num_intervals):
    """
    Generate list of gaussian kernels at which to blur the input image. Default values of sigma, intervals, and octaves follow section 3 of Lowe's paper.
    in the paper: The initial image is incrementally convolved with Gaussians to produce images separated by a constant factor k in scale space inside an octave.
    """
    logger.debug('Generating scales...')
    #  We must produce s + 3 images in the stack of blurred images for each octave,
    #  so that extrema detection covers a complete octave.
    num_images_per_octave = num_intervals + 3
    #  We choose to divide each octave of scale space (i.e., doubling of σ) into an integer number, num_intervals, of intervals, so k = 2^1/num_intervals .
    k = 2 ** (1. / num_intervals)
    # scale of gaussian blur necessary to go from one blur scale to the next within an octave
    gaussian_kernels = zeros(num_images_per_octave)
    gaussian_kernels[0] = sigma

    for image_index in range(1, num_images_per_octave):
        sigma_previous = (k ** (image_index - 1)) * sigma
        sigma_total = k * sigma_previous
        #  blurring an input image by kernel size σ₁ and then blurring the resulting image by σ₂ is equivalent to blurring the input image just once by σ, where σ² = σ₁² + σ₂².
        #  proof see:https://math.stackexchange.com/questions/3159846/what-is-the-resulting-sigma-after-applying-successive-gaussian-blur
        gaussian_kernels[image_index] = sqrt(sigma_total ** 2 - sigma_previous ** 2)
    return gaussian_kernels


def generateGaussianImages(image, num_octaves, gaussian_kernels):
    """
    Generate scale-space pyramid of Gaussian images
    """
    logger.debug('Generating Gaussian images...')
    gaussian_images = []

    for octave_index in range(num_octaves):
        gaussian_images_in_octave = [image]  # first image in octave already has the correct blur
        for gaussian_kernel in gaussian_kernels[1:]:
            image = GaussianBlur(image, (0, 0), sigmaX=gaussian_kernel, sigmaY=gaussian_kernel)
            gaussian_images_in_octave.append(image)
        gaussian_images.append(gaussian_images_in_octave)
        octave_base = gaussian_images_in_octave[-3]
        # Since we generate s+3 images per octave, we use the third to last image
        # as the base for the next octave since that is the one with a blur of 2*sigma.
        # in the paper: Once a complete octave has been processed, we resample the Gaussian image that has twice the initial value of σ (it will be 2 images from the top of the stack) by taking every second pixel in each row and column.
        # another way of implement: image = gaussian_images_in_octave[-3][::2][::2].
        image = resize(octave_base,
                       (int(octave_base.shape[1] / 2),
                        int(octave_base.shape[0] / 2)),
                       interpolation=INTER_NEAREST)
    return array(gaussian_images, dtype=object)


def generateDoGImages(gaussian_images):
    """Generate Difference-of-Gaussians image pyramid
    """
    logger.debug('Generating Difference-of-Gaussian images...')
    dog_images = []

    for gaussian_images_in_octave in gaussian_images:
        dog_images_in_octave = []
        # zip function will auto clip tail for those too long list.
        for first_image, second_image in zip(gaussian_images_in_octave,
                                             gaussian_images_in_octave[1:]):
            # ordinary subtraction will work since we have cast the images of unsigned integers(uint) to 'float32' by image = image.astype('float32')
            # cv.subtract will clip the negative value to 0.
            dog_images_in_octave.append(
                subtract(second_image, first_image))
        dog_images.append(dog_images_in_octave)
    return array(dog_images, dtype=object)


###############################
# Scale-space extrema related #
###############################

def findScaleSpaceExtrema(gaussian_images, dog_images, num_intervals, sigma, image_border_width,
                          contrast_threshold=0.04):
    """
    Find pixel positions of all scale-space extrema in the image pyramid
    """
    logger.debug('Finding scale-space extrema...')
    threshold = floor(0.5 * contrast_threshold / num_intervals * 255)  # from OpenCV implementation
    keypoints = []

    # each triplet of images, we look for pixels in the middle image that are greater than or less than all of their 26 neighbors: 8 neighbors in the middle image, 9 neighbors in the image below, and 9 neighbors in the image above.
    for octave_index, dog_images_in_octave in enumerate(dog_images):
        for image_index, (first_image, second_image, third_image) in enumerate(
                zip(dog_images_in_octave, dog_images_in_octave[1:], dog_images_in_octave[2:])):
            # (i, j) is the center of the 3x3 array
            for i in range(image_border_width, first_image.shape[0] - image_border_width):
                for j in range(image_border_width,
                               first_image.shape[1] - image_border_width):
                    if isPixelAnExtremum(first_image[i - 1:i + 2, j - 1:j + 2],
                                         second_image[i - 1:i + 2, j - 1:j + 2],
                                         third_image[i - 1:i + 2, j - 1:j + 2],
                                         threshold):
                        localization_result = \
                            localizeExtremumViaQuadraticFit(i, j,
                                                            image_index + 1,
                                                            octave_index,
                                                            num_intervals,
                                                            dog_images_in_octave,
                                                            sigma, contrast_threshold,
                                                            image_border_width)
                        if localization_result is not None:
                            keypoint, localized_image_index = localization_result
                            keypoints_with_orientations = computeKeypointsWithOrientations(
                                keypoint, octave_index,
                                gaussian_images[octave_index][localized_image_index])
                            keypoints += keypoints_with_orientations
    return keypoints


def isPixelAnExtremum(first_subimage, second_subimage, third_subimage, threshold):
    """Return True if the center element of the 3x3x3 input array is strictly greater than or less than all its neighbors, False otherwise
    """
    center_pixel_value = second_subimage[1, 1]
    if abs(center_pixel_value) > threshold:
        if center_pixel_value > 0:  # check for maxima
            return all(center_pixel_value >= first_subimage) and \
                   all(center_pixel_value >= third_subimage) and \
                   all(center_pixel_value >= second_subimage)
        elif center_pixel_value < 0:  # check for minima
            return all(center_pixel_value <= first_subimage) and \
                   all(center_pixel_value <= third_subimage) and \
                   all(center_pixel_value <= second_subimage)
    return False


def localizeExtremumViaQuadraticFit(i, j, image_index, octave_index, num_intervals, dog_images_in_octave, sigma,
                                    contrast_threshold, image_border_width, eigenvalue_ratio=10,
                                    num_attempts_until_convergence=5):
    """
    Iteratively refine pixel positions of scale-space extrema via quadratic fit around each extremum's neighbors.
    fit a quadratic model to the input keypoint pixel and all 26 of its neighboring pixels (we call this a pixel_cube). We update the keypoint’s position with the subpixel-accurate extremum estimated from this model.
    subpixel====pixels that are not sampled in the continues function.
    """
    logger.debug('Localizing scale-space extrema...')
    extremum_is_outside_image = False
    image_shape = dog_images_in_octave[0].shape

    # this loop attempts to update(fine-tune) the location of candidate keypoints.
    for attempt_index in range(num_attempts_until_convergence):
        # need to convert from uint8 to float32 to compute derivatives and need to rescale pixel values to [0, 1] to apply Lowe's thresholds
        first_image, second_image, third_image = dog_images_in_octave[image_index - 1:image_index + 2]
        pixel_cube = stack([first_image[i - 1:i + 2, j - 1:j + 2],
                            second_image[i - 1:i + 2, j - 1:j + 2],
                            third_image[i - 1:i + 2, j - 1:j + 2]]).astype('float32') / 255.
        gradient = computeGradientAtCenterPixel(pixel_cube)
        hessian = computeHessianAtCenterPixel(pixel_cube)

        # Return the least-squares solution to a linear matrix equation.
        # Computes the vector x that approximatively solves the equation a @ x = b.
        # The equation may be under-, well-, or over-determined (i.e., the number of linearly
        # independent rows of a can be less than, equal to, or greater than its number of linearly
        # independent columns). If a is square and of full rank, then x (but for round-off error)
        # is the "exact" solution of the equation. Else, x minimizes the Euclidean l2-norm.
        # this is the offset(update) to the candidate extreme points
        extremum_update = -lstsq(hessian, gradient, rcond=None)[0]

        # if the offset is sufficiently small, no need to update the extreme point, break.
        if np.all(abs(extremum_update) < 0.5): break

        # else update the extremum for x, y and s:
        j += int(round(extremum_update[0]))  # round to the nearest int: 1.5-->2, 1.4-->1.
        i += int(round(extremum_update[1]))
        image_index += int(round(extremum_update[2]))

        # make sure the new pixel_cube will lie entirely within the image
        if i < image_border_width or i >= image_shape[0] - image_border_width or j < image_border_width or j >= \
                image_shape[1] - image_border_width or image_index < 1 or image_index > num_intervals:
            extremum_is_outside_image = True
            break

    if extremum_is_outside_image:
        logger.debug('Updated extremum moved outside of image before reaching convergence. Skipping...')
        return None
    if attempt_index >= num_attempts_until_convergence - 1:
        logger.debug('Exceeded maximum number of attempts without reaching convergence for this extremum. Skipping...')
        return None

    # get the estimate continue function value at final updated extreme sub-pixel.
    functionValueAtUpdatedExtremum = pixel_cube[1, 1, 1] + 0.5 * dot(gradient, extremum_update)

    #  reject low contrast
    #  recall laplace of gaussian, a high value is more likely a corner keypoint.
    if abs(functionValueAtUpdatedExtremum) >= contrast_threshold / num_intervals:
        xy_hessian = hessian[:2, :2]
        xy_hessian_trace = trace(xy_hessian)
        xy_hessian_det = det(xy_hessian)

        # discard point of different sign eigenvalues for the hessian.
        if xy_hessian_det > 0 and eigenvalue_ratio * (xy_hessian_trace ** 2) < (
                (eigenvalue_ratio + 1) ** 2) * xy_hessian_det:
            # Edge check passed -- construct and return OpenCV KeyPoint object
            # The KeyPoint class instance stores a keypoint, i.e. a point feature found by one of many available keypoint detectors, such as Harris corner detector, FAST, StarDetector, SURF, SIFT etc.
            # The keypoint is characterized by the 2D position, scale (proportional to the diameter of the neighborhood that needs to be taken into account), orientation and some other parameters. The keypoint neighborhood is then analyzed by another algorithm that builds a descriptor (usually represented as a feature vector). The keypoints representing the same object in different images can then be matched using KDTree or another method.
            keypoint = KeyPoint()

            # The following code is according to the opencv cpp code.
            # kpt.pt.x = (c + xc) * (1 << octv);
            # kpt.pt.y = (r + xr) * (1 << octv);
            # kpt.octave = octv + (layer << 8) + (cvRound((xi + 0.5) * 255) << 16);
            # kpt.size = sigma * powf(2.f, (layer + xi) / nOctaveLayers)*(1 << octv) * 2;
            # kpt.response = std::abs(contr)
            keypoint.pt = (
                (j + extremum_update[0]) * (2 ** octave_index), (i + extremum_update[1]) * (2 ** octave_index))
            #  kpt.octave = octv + (layer << 8) + (cvRound((xi + 0.5)*255) << 16);
            keypoint.octave = octave_index + image_index * (2 ** 8) + int(round((extremum_update[2] + 0.5) * 255)) * (
                        2 ** 16)
            keypoint.size = sigma * (2 ** ((image_index + extremum_update[2]) / float32(num_intervals))) * (
                        2 ** (octave_index + 1))  # octave_index + 1 because the input image was doubled
            keypoint.response = abs(functionValueAtUpdatedExtremum)
            return keypoint, image_index
    return None


def computeGradientAtCenterPixel(pixel_array):
    """Approximate gradient at center pixel [1, 1, 1] of 3x3x3 array using central difference formula of order O(h^2), where h is the step size
    """
    # With step size h, the central difference formula of order O(h^2) for f'(x) is (f(x + h) - f(x - h)) / (2 * h)
    # Here h = 1, so the formula simplifies to f'(x) = (f(x + 1) - f(x - 1)) / 2
    # NOTE: x corresponds to second array axis, y corresponds to first array axis, and s (scale) corresponds to third array axis
    dx = 0.5 * (pixel_array[1, 1, 2] - pixel_array[1, 1, 0])
    dy = 0.5 * (pixel_array[1, 2, 1] - pixel_array[1, 0, 1])
    ds = 0.5 * (pixel_array[2, 1, 1] - pixel_array[0, 1, 1])
    return array([dx, dy, ds])


def computeHessianAtCenterPixel(pixel_array):
    """Approximate Hessian at center pixel [1, 1, 1] of 3x3x3 array using central difference formula of order O(h^2), where h is the step size.
    the Hessian matrix or Hessian is a square matrix of second-order partial derivatives of a scalar-valued function
    """
    # With step size h, the central difference formula of order O(h^2) for f''(x) is (f(x + h) - 2 * f(x) + f(x - h)) / (h ^ 2)
    # Here h = 1, so the formula simplifies to f''(x) = f(x + 1) - 2 * f(x) + f(x - 1)
    # With step size h, the central difference formula of order O(h^2) for (d^2) f(x, y) / (dx dy) = (f(x + h, y + h) - f(x + h, y - h) - f(x - h, y + h) + f(x - h, y - h)) / (4 * h ^ 2)
    # Here h = 1, so the formula simplifies to (d^2) f(x, y) / (dx dy) = (f(x + 1, y + 1) - f(x + 1, y - 1) - f(x - 1, y + 1) + f(x - 1, y - 1)) / 4

    # pixel array shape: [s,h,w] ==> [s,y,x]
    # center_pixel_value = pixel_array[1, 1, 1]
    dxx = pixel_array[1, 1, 2] + pixel_array[1, 1, 0] - 2 * center_pixel_value
    dyy = pixel_array[1, 2, 1] + pixel_array[1, 0, 1] - 2 * center_pixel_value
    dss = pixel_array[2, 1, 1] + pixel_array[0, 1, 1] - 2 * center_pixel_value
    dxy = 0.25 * (pixel_array[1, 2, 2] - pixel_array[1, 2, 0] - pixel_array[1, 0, 2] + pixel_array[1, 0, 0])
    dxs = 0.25 * (pixel_array[2, 1, 2] - pixel_array[2, 1, 0] - pixel_array[0, 1, 2] + pixel_array[0, 1, 0])
    dys = 0.25 * (pixel_array[2, 2, 1] - pixel_array[2, 0, 1] - pixel_array[0, 2, 1] + pixel_array[0, 0, 1])
    return array([[dxx, dxy, dxs],
                  [dxy, dyy, dys],
                  [dxs, dys, dss]])


#########################
# Keypoint orientations #
#########################

def computeKeypointsWithOrientations(keypoint, octave_index, gaussian_image, radius_factor=3, num_bins=36,
                                     peak_ratio=0.8, scale_factor=1.5):
    """
    Assigning a consistent orientation to each keypoint based on local image properties。
    the keypoint descriptor can be represented relative to this orientation and therefore achieve invariance to image rotation.
    """
    logger.debug('Computing keypoint orientations...')
    keypoints_with_orientations = []
    image_shape = gaussian_image.shape

    #  The scale of the keypoint is used to select the Gaussian smoothed image, L, with the closest scale, so that all computations are performed in a scale-invariant manner.
    scale = scale_factor * keypoint.size / float32(
        2 ** (octave_index + 1))  # compare with keypoint.size computation in localizeExtremumViaQuadraticFit()
    radius = int(round(radius_factor * scale))

    raw_histogram = zeros(num_bins)
    smooth_histogram = zeros(num_bins)

    # An orientation histogram is formed from the gradient orientations of sample points within a region around the keypoint.
    # use a square neighborhood here, as the OpenCV implementation does.
    for i in range(-radius, radius + 1):
        region_y = int(round(keypoint.pt[1] / float32(2 ** octave_index))) + i
        if 0 < region_y < image_shape[0] - 1:  # -1 to make sure it has gradient
            for j in range(-radius, radius + 1):
                region_x = int(round(keypoint.pt[0] / float32(2 ** octave_index))) + j
                if 0 < region_x < image_shape[1] - 1:  # -1 to make sure it has gradient
                    dx = gaussian_image[region_y, region_x + 1] - gaussian_image[region_y, region_x - 1]
                    dy = gaussian_image[region_y - 1, region_x] - gaussian_image[region_y + 1, region_x]
                    gradient_magnitude = sqrt(dx * dx + dy * dy)
                    gradient_orientation = rad2deg(arctan2(dy, dx))
                    # Each sample added to the histogram is weighted by its gradient magnitude and by a Gaussian-weighted circular window with a σ that is 1.5 times that of the scale of the keypoint.
                    # constant in front of 2d gaussian formula's exponential can be dropped
                    # For a bigger distance, the value of the Pixel will be less weighted ==> pixels farther from the keypoint have less of an influence on the histogram.
                    weight = exp(-0.5 * (i ** 2 + j ** 2) / (scale ** 2))
                    histogram_index = int(round(gradient_orientation * num_bins / 360.))
                    raw_histogram[histogram_index % num_bins] += weight * gradient_magnitude

    # smooth the histogram. The smoothing coefficients correspond to a 5-point Gaussian filter
    # 1d 5-point Gaussian filter ==> 杨辉三角第5层[1,4,6,4,1]
    for n in range(num_bins):
        smooth_histogram[n] = (6 * raw_histogram[n] + 4 * (raw_histogram[n - 1] + raw_histogram[(n + 1) % num_bins]) +
                               raw_histogram[n - 2] + raw_histogram[(n + 2) % num_bins]) / 16.
    orientation_max = max(smooth_histogram)
    # np.roll: right or left shift like circle.
    # generate local peak: bigger than its left and right.
    orientation_peaks = where(logical_and(smooth_histogram > roll(smooth_histogram, 1),
                                          smooth_histogram > roll(smooth_histogram, -1)))[0]
    for peak_index in orientation_peaks:
        peak_value = smooth_histogram[peak_index]
        if peak_value >= peak_ratio * orientation_max:
            # a parabola（抛物线） is ﬁt to the 3 histogram values closest to each peak to interpolate the peak position for better accuracy.
            # The interpolation update is given by equation (6.30) in https://ccrma.stanford.edu/~jos/sasp/Quadratic_Interpolation_Spectral_Peaks.html
            left_value = smooth_histogram[(peak_index - 1) % num_bins]
            right_value = smooth_histogram[(peak_index + 1) % num_bins]
            interpolated_peak_index = (peak_index + 0.5 * (left_value - right_value) / (
                    left_value - 2 * peak_value + right_value)) % num_bins
            orientation = 360. - interpolated_peak_index * 360. / num_bins
            if abs(orientation - 360.) < float_tolerance:
                orientation = 0
            new_keypoint = KeyPoint(*keypoint.pt, keypoint.size, orientation, keypoint.response, keypoint.octave)
            keypoints_with_orientations.append(new_keypoint)
    return keypoints_with_orientations


##############################
# Duplicate keypoint removal #
##############################

def compareKeypoints(keypoint1, keypoint2):
    """Return True if keypoint1 is less than keypoint2
    """
    if keypoint1.pt[0] != keypoint2.pt[0]:
        return keypoint1.pt[0] - keypoint2.pt[0]
    if keypoint1.pt[1] != keypoint2.pt[1]:
        return keypoint1.pt[1] - keypoint2.pt[1]
    if keypoint1.size != keypoint2.size:
        return keypoint2.size - keypoint1.size
    if keypoint1.angle != keypoint2.angle:
        return keypoint1.angle - keypoint2.angle
    if keypoint1.response != keypoint2.response:
        return keypoint2.response - keypoint1.response
    if keypoint1.octave != keypoint2.octave:
        return keypoint2.octave - keypoint1.octave
    return keypoint2.class_id - keypoint1.class_id


def removeDuplicateKeypoints(keypoints):
    """Sort keypoints and remove duplicate keypoints
    """
    if len(keypoints) < 2:
        return keypoints

    keypoints.sort(key=cmp_to_key(compareKeypoints))
    unique_keypoints = [keypoints[0]]

    for next_keypoint in keypoints[1:]:
        last_unique_keypoint = unique_keypoints[-1]
        if last_unique_keypoint.pt[0] != next_keypoint.pt[0] or \
                last_unique_keypoint.pt[1] != next_keypoint.pt[1] or \
                last_unique_keypoint.size != next_keypoint.size or \
                last_unique_keypoint.angle != next_keypoint.angle:
            unique_keypoints.append(next_keypoint)
    return unique_keypoints


#############################
# Keypoint scale conversion #
#############################

def convertKeypointsToInputImageSize(keypoints):
    """
    Convert keypoint point, size, and octave to input image size
    """
    converted_keypoints = []
    for keypoint in keypoints:
        keypoint.pt = tuple(0.5 * array(keypoint.pt))
        keypoint.size *= 0.5
        keypoint.octave = (keypoint.octave & ~255) | ((keypoint.octave - 1) & 255)
        converted_keypoints.append(keypoint)
    return converted_keypoints


#########################
# Descriptor generation #
#########################

def unpackOctave(keypoint):
    """Compute octave, layer, and scale from a keypoint
    """
    octave = keypoint.octave & 255
    layer = (keypoint.octave >> 8) & 255
    if octave >= 128:
        octave = octave | -128
    scale = 1 / float32(1 << octave) if octave >= 0 else float32(1 << -octave)
    return octave, layer, scale


def generateDescriptors(keypoints, gaussian_images, window_width=4, num_bins=8, scale_multiplier=3,
                        descriptor_max_value=0.2):
    """
    Generate descriptors for each keypoint。
    A keypoint descriptor is created by ﬁrst computing the gradient magnitude and orientation at each image sample point in a region around the keypoint location(a 16x16 patch is inspected around each keypoint, That patch is then split up into 16 4x4 subregions. The gradients (in polar coordinates) of each subregion are then binned into an 8-bin histogram.). These are weighted by a Gaussian window。
    """
    logger.debug('Generating descriptors...')
    descriptors = []

    for keypoint in keypoints:
        octave, layer, scale = unpackOctave(keypoint)
        # 特征描述子与特征点所在的尺度有关，因此，对梯度的求取应在特征点对应的高斯图像上进行。
        gaussian_image = gaussian_images[octave + 1, layer]
        num_rows, num_cols = gaussian_image.shape
        point = round(scale * array(keypoint.pt)).astype('int')
        degrees_per_bin = 360. / num_bins
        angle = 360. - keypoint.angle
        cos_angle = cos(deg2rad(angle))
        sin_angle = sin(deg2rad(angle))
        weight_multiplier = -0.5 / ((0.5 * window_width) ** 2)
        row_bin_list = []
        col_bin_list = []
        magnitude_list = []
        orientation_bin_list = []
        # SIFT描述子 h (x, y, θ) 是对关键点附近邻域内高斯图像梯度统计的结果，是一个三维矩阵(shape=[4,4,8])，但通常用一个矢量来表示。特征向量通过对三维矩阵按一定规律排列得到。
        histogram_tensor = zeros((window_width + 2,
                                  window_width + 2,
                                  num_bins))  # first two dimensions are increased by 2 to account for border effects

        # Descriptor window size (described by half_width) follows OpenCV convention
        # 将关键点附近的邻域划分为d*d(Lowe建议d=4)个子区域，每个子区域做为一个种子点，每个种子点有8个方向。每个子区域的大小与关键点方向分配时相同，即每个区域有个scale_multiplier*scale个子像素，为每个子区域分配边长为scale_multiplier*scale的矩形区域进行采样(scale_multiplier*scale个子像素实际用边长为sqrt(scale_multiplier*scale)的矩形区域即可包含，但为了简化计算取其边长为，并且采样点宜多不宜少)。
        # 考虑到实际计算时，需要采用双线性插值，所需图像窗口边长为scale_multiplier*scale*(d+1)。
        # 在考虑到旋转因素(方便下一步将坐标轴旋转到关键点的方向)，实际计算所需的图像区域半径为：
        # scale_multiplier * scale * (d+1)* sqrt(2) / 2
        hist_width = scale_multiplier * 0.5 * scale * keypoint.size
        # sqrt(2) corresponds to diagonal length of a pixel
        half_width = int(round(hist_width * sqrt(2) * (window_width + 1) * 0.5))
        # ensure half_width lies within image
        half_width = int(min(half_width, sqrt(num_rows ** 2 + num_cols ** 2)))

        # refer:https://blog.csdn.net/zddblog/article/details/7521424
        for row in range(-half_width, half_width + 1):
            for col in range(-half_width, half_width + 1):
                # step1: 把特征点的主方向旋转到x轴方向，同时旋转区域内的像素点
                # The coordinates of the point after rotation are :
                # y' = xsin(θ)+ycos(θ); x'= xcos(θ)-ysin(θ)
                row_rot = col * sin_angle + row * cos_angle
                col_rot = col * cos_angle - row * sin_angle
                # 旋转后的采样点坐标在半径为radius的圆内被分配到的子区域，计算影响子区域的采样点的梯度和方向，分配到8个方向上。
                # 旋转后的采样点落在子区域的下标为（判断落在哪个子区域）:
                # + 0.5 * window_width：因为中心的子区域坐标为（0.5 * window_width，0.5 * window_width）
                row_bin = (row_rot / hist_width) + 0.5 * window_width - 0.5  # 前面用的(d+1)/2，多了0.5
                col_bin = (col_rot / hist_width) + 0.5 * window_width - 0.5
                if -1 < row_bin < window_width and -1 < col_bin < window_width:
                    # current point's coordinates on the image
                    window_row = int(round(point[1] + row))
                    window_col = int(round(point[0] + col))
                    if 0 < window_row < num_rows - 1 and 0 < window_col < num_cols - 1:
                        dx = gaussian_image[window_row, window_col + 1] - gaussian_image[window_row, window_col - 1]
                        dy = gaussian_image[window_row - 1, window_col] - gaussian_image[window_row + 1, window_col]
                        gradient_magnitude = sqrt(dx * dx + dy * dy)
                        gradient_orientation = rad2deg(arctan2(dy, dx)) % 360
                        weight = exp(weight_multiplier * ((row_rot / hist_width) ** 2 + (col_rot / hist_width) ** 2))
                        # hist index (row, col, orientation) and value( weighted gradient_magnitude).
                        row_bin_list.append(row_bin)
                        col_bin_list.append(col_bin)
                        orientation_bin_list.append((gradient_orientation - angle) / degrees_per_bin)
                        magnitude_list.append(weight * gradient_magnitude)

        for row_bin, col_bin, magnitude, orientation_bin in zip(row_bin_list,
                                                                col_bin_list,
                                                                magnitude_list,
                                                                orientation_bin_list):
            # Smoothing via trilinear interpolation
            # Notations follows https://en.wikipedia.org/wiki/Trilinear_interpolation
            # Note that we are really doing the inverse of trilinear interpolation here (we take the center value of the cube and distribute it among its eight neighbors)
            row_bin_floor, col_bin_floor, orientation_bin_floor = floor([row_bin, col_bin, orientation_bin]).astype(int)
            row_fraction, col_fraction, orientation_fraction = row_bin - row_bin_floor, col_bin - col_bin_floor, orientation_bin - orientation_bin_floor
            orientation_bin_floor = orientation_bin_floor % num_bins  # make sure in range[0, num_bins]

            c1 = magnitude * row_fraction
            c0 = magnitude * (1 - row_fraction)
            c11 = c1 * col_fraction
            c10 = c1 * (1 - col_fraction)
            c01 = c0 * col_fraction
            c00 = c0 * (1 - col_fraction)
            c111 = c11 * orientation_fraction
            c110 = c11 * (1 - orientation_fraction)
            c101 = c10 * orientation_fraction
            c100 = c10 * (1 - orientation_fraction)
            c011 = c01 * orientation_fraction
            c010 = c01 * (1 - orientation_fraction)
            c001 = c00 * orientation_fraction
            c000 = c00 * (1 - orientation_fraction)

            histogram_tensor[row_bin_floor + 1, col_bin_floor + 1, orientation_bin_floor] += c000
            histogram_tensor[row_bin_floor + 1, col_bin_floor + 1, (orientation_bin_floor + 1) % num_bins] += c001
            histogram_tensor[row_bin_floor + 1, col_bin_floor + 2, orientation_bin_floor] += c010
            histogram_tensor[row_bin_floor + 1, col_bin_floor + 2, (orientation_bin_floor + 1) % num_bins] += c011
            histogram_tensor[row_bin_floor + 2, col_bin_floor + 1, orientation_bin_floor] += c100
            histogram_tensor[row_bin_floor + 2, col_bin_floor + 1, (orientation_bin_floor + 1) % num_bins] += c101
            histogram_tensor[row_bin_floor + 2, col_bin_floor + 2, orientation_bin_floor] += c110
            histogram_tensor[row_bin_floor + 2, col_bin_floor + 2, (orientation_bin_floor + 1) % num_bins] += c111

        descriptor_vector = histogram_tensor[1:-1, 1:-1, :].flatten()  # Remove histogram borders
        # Threshold and normalize descriptor_vector
        threshold = norm(descriptor_vector) * descriptor_max_value
        descriptor_vector[descriptor_vector > threshold] = threshold
        descriptor_vector /= max(norm(descriptor_vector), float_tolerance)
        # Multiply by 512, round, and saturate between 0 and 255 to convert from float32 to unsigned char (OpenCV convention)
        descriptor_vector = round(512 * descriptor_vector)
        descriptor_vector[descriptor_vector < 0] = 0
        descriptor_vector[descriptor_vector > 255] = 255
        descriptors.append(descriptor_vector)
    return array(descriptors, dtype='float32')
